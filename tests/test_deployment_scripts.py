import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEST_SUPABASE_PUBLISHABLE_KEY = (
    "sb_publishable_1234567890abcdefghij_12345678"
)


def test_pytest_discovery_is_scoped_to_the_tracked_application_suite() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '[tool.pytest.ini_options]\ntestpaths = ["tests"]' in pyproject


def _load_hosted_verifier():
    script_path = ROOT / "scripts/verify_hosted_local.py"
    spec = importlib.util.spec_from_file_location("verify_hosted_local", script_path)
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    return verifier


def _load_edge_pairing_creator():
    script_path = ROOT / "scripts/create-edge-pairing.py"
    spec = importlib.util.spec_from_file_location("create_edge_pairing", script_path)
    assert spec is not None and spec.loader is not None
    creator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(creator)
    return creator


def test_edge_pairing_creator_only_sends_digest_and_writes_private_file(
    tmp_path,
    monkeypatch,
) -> None:
    creator = _load_edge_pairing_creator()
    requests = []

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr(creator.secrets, "token_urlsafe", lambda _size: "one-use-code")
    monkeypatch.setattr(creator.urllib.request, "urlopen", fake_urlopen)

    code, _expires_at = creator.create_pairing(
        supabase_url="https://project.supabase.co",
        service_role_key="service-secret",
        workspace_id="workspace-1",
        device_id="main-cabinet",
    )
    output = tmp_path / "pairing-code"
    creator.write_secret(output, code)

    payload = json.loads(requests[0][0].data)
    assert payload["pairing_hash"] != code
    assert code not in requests[0][0].data.decode("utf-8")
    assert output.read_text(encoding="utf-8") == f"{code}\n"
    assert output.stat().st_mode & 0o777 == 0o600


def test_hosted_verifier_requires_complete_client_surfaces(monkeypatch) -> None:
    verifier = _load_hosted_verifier()
    bodies = {
        "/mobile": (
            b'<textarea id="command"></textarea><nav data-product-nav></nav>'
            b'place_configuration_ui_required result.places_url'
            b'<section id="offlineQueue"></section>indexedDB.open navigator.onLine'
            b'navigator.serviceWorker.register'
        ),
        "/storage?view=places": (
            b'<dialog id="storageSetupDialog"></dialog>'
            b'<button id="storageSetupSubmit"></button>api("/storage/setup"'
        ),
        "/kiosk": (
            b'<main id="kioskMain"></main><div id="kioskActions"></div>'
            b'place_configuration_ui_required voice_scope_ui_required'
        ),
        "/account": (
            b'<form id="deleteWorkspaceForm"></form>'
            b'<input id="deleteWorkspaceConfirmation">'
            b'<button id="leaveWorkspaceButton"></button>'
            b'<script src="/auth.js?v=verified-build"></script>'
        ),
    }
    requested: list[str] = []

    def fake_request_bytes(method, url, *, headers=None, expected={200}):
        del method, headers, expected
        route = url.removeprefix("https://magpie.example")
        requested.append(route)
        return 200, {"content-type": "text/html; charset=utf-8"}, bodies[route]

    monkeypatch.setattr(verifier, "request_bytes", fake_request_bytes)
    verifier.verify_client_surfaces(
        "https://magpie.example",
        {"Authorization": "Bearer test"},
    )

    assert requested == ["/mobile", "/storage?view=places", "/kiosk", "/account"]

    bodies["/kiosk"] = b'<main id="kioskMain"></main>'
    with pytest.raises(verifier.SmokeFailure, match="/kiosk is incomplete"):
        verifier.verify_client_surfaces(
            "https://magpie.example",
            {"Authorization": "Bearer test"},
        )


def test_hosted_verifier_completes_due_physical_storage_checks(monkeypatch) -> None:
    verifier = _load_hosted_verifier()
    headers = {"Authorization": "Bearer test"}
    verified: list[dict[str, object]] = []

    def fake_request_json(
        method,
        url,
        *,
        payload=None,
        headers=None,
        expected={200},
    ):
        del expected
        assert headers == {"Authorization": "Bearer test"}
        if url.endswith("/review/queue?limit=50"):
            return 200, {
                "place_checks": []
                if verified
                else [{"location_id": "metric hardware"}]
            }
        if url.endswith("/locations/metric%20hardware/audit"):
            return 200, {
                "can_verify": True,
                "revision": "revision-1",
                "placements": [
                    {"item_id": 42, "location_id": "metric hardware"}
                ],
            }
        if method == "POST" and url.endswith(
            "/locations/metric%20hardware/verify"
        ):
            verified.append(payload)
            return 200, {"ok": True}
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(verifier, "request_json", fake_request_json)

    verifier.exercise_physical_storage_checks(
        "https://magpie.example",
        headers,
    )

    assert verified == [
        {
            "revision": "revision-1",
            "placements": [
                {
                    "item_id": 42,
                    "location_id": "metric hardware",
                    "status": "present",
                }
            ],
        }
    ]


def test_hosted_verifier_requires_public_pwa_install_contract(monkeypatch) -> None:
    verifier = _load_hosted_verifier()
    manifest = {
        "name": "Magpie Storage",
        "start_url": "/mobile?source=pwa",
        "scope": "/",
        "display": "standalone",
        "icons": [
            {"src": "/app-icon-192.png", "sizes": "192x192", "purpose": "any"},
            {"src": "/app-icon-512.png", "sizes": "512x512", "purpose": "any"},
            {
                "src": "/app-icon-512.png",
                "sizes": "512x512",
                "purpose": "maskable",
            },
        ],
        "shortcuts": [
            {"url": "/mobile?action=find&source=shortcut"},
            {"url": "/mobile?action=put-away&source=shortcut"},
            {"url": "/storage?view=inventory&source=shortcut"},
            {"url": "/storage?view=places&create=top&source=shortcut"},
        ],
    }
    worker = (
        b'const APP_SHELL = ["/mobile", "/storage", "/auth.js?v=verified-build"];'
        b'self.addEventListener("install",()=>skipWaiting());'
        b'self.addEventListener("activate",()=>clients.claim());'
        b'self.addEventListener("fetch",()=>{});CACHE_SCAN_PAGE'
    )
    png = b"\x89PNG\r\n\x1a\nimage"
    requested: list[str] = []

    def fake_request_json(method, url, **kwargs):
        del method, kwargs
        requested.append(url.removeprefix("https://magpie.example"))
        return 200, manifest

    def fake_request_bytes(method, url, *, headers=None, expected={200}):
        del method, headers, expected
        route = url.removeprefix("https://magpie.example")
        requested.append(route)
        if route == "/service-worker.js":
            return 200, {
                "content-type": "text/javascript; charset=utf-8",
                "cache-control": "no-cache, no-store, must-revalidate",
            }, worker
        return 200, {"content-type": "image/png"}, png

    monkeypatch.setattr(verifier, "request_json", fake_request_json)
    monkeypatch.setattr(verifier, "request_bytes", fake_request_bytes)
    verifier.verify_pwa_contract("https://magpie.example")

    assert requested == [
        "/manifest.webmanifest",
        "/service-worker.js",
        "/app-icon-192.png",
        "/app-icon-512.png",
        "/apple-touch-icon.png",
    ]

    manifest["start_url"] = "/"
    with pytest.raises(verifier.SmokeFailure, match="invalid launch contract"):
        verifier.verify_pwa_contract("https://magpie.example")


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _supabase_test_app(tmp_path: Path, *, migration_exit: int = 0) -> tuple[Path, Path]:
    app = tmp_path / "app"
    (app / "config").mkdir(parents=True)
    (app / "state").mkdir()
    (app / "config/led-controller.json").write_text(
        json.dumps(
            {
                "database_path": "state/inventory.db",
                "zebra": {"base_url": "http://old.example"},
            }
        ),
        encoding="utf-8",
    )
    (app / "state/inventory.db").write_bytes(b"sqlite-data")
    log = tmp_path / "commands.log"
    fake_bin = tmp_path / "bin"
    _executable(
        fake_bin / "systemctl",
        f"#!/bin/sh\nprintf 'systemctl %s\\n' \"$*\" >> {log}\n",
    )
    _executable(
        fake_bin / "curl",
        "#!/bin/sh\nprintf '%s\\n' '{\"storage\":{\"backend\":\"supabase\",\"workspace_id\":\"magpie\"}}'\n",
    )
    _executable(
        app / ".venv/bin/searchable-storage-migrate-supabase",
        f"#!/bin/sh\nprintf 'migrate %s\\n' \"$*\" >> {log}\nexit {migration_exit}\n",
    )
    _executable(
        app / ".venv/bin/searchable-storage-supabase-check",
        f"#!/bin/sh\nprintf 'check %s\\n' \"$*\" >> {log}\n",
    )
    return app, log


def _supabase_env(app: Path, tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "APP_DIR": str(app),
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "SUPABASE_URL": "https://project.supabase.co",
        "SUPABASE_WORKSPACE_ID": "magpie",
        "SUPABASE_SERVICE_ROLE_KEY": "service-role-secret",
        "MAGPIE_PUBLIC_BASE_URL": "https://magpie.example",
    }


def test_supabase_switch_script_has_backup_verify_and_rollback_guards() -> None:
    script = (ROOT / "scripts/configure-pi-supabase.sh").read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert "inventory.pre-supabase-" in script
    assert "led-controller.pre-supabase-" in script
    assert "systemctl --user stop" in script
    assert "service_stopped=true" in script
    assert "searchable-storage-migrate-supabase" in script
    assert "searchable-storage-supabase-check" in script
    assert 'path.chmod(0o600)' in script
    assert 'storage.get("backend") != "supabase"' in script
    assert "rollback" in script


def test_runtime_storage_preflight_checks_supabase_without_mutating_sqlite() -> None:
    script = (ROOT / "scripts/verify-runtime-storage.sh").read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert "LedConfig.from_path" in script
    assert 'if [[ "$backend" != "supabase" ]]' in script
    assert 'source "$ENV_PATH"' in script
    assert "searchable-storage-supabase-check" in script




def test_mobile_deployment_keeps_customer_and_edge_boundaries_separate() -> None:
    guide = (ROOT / "docs/mobile-deployment.md").read_text(encoding="utf-8")
    pi_installer = (ROOT / "scripts/install-pi5-cloudflare-tunnel.sh").read_text(
        encoding="utf-8"
    )

    assert "Leave that customer hostname public" in guide
    assert "Protect only the edge hostname" in guide
    assert "Do not route the customer hostname to the Pi" in guide
    assert "MAGPIE_ORIGIN_MODE=public" in guide
    assert "MAGPIE_ORIGIN_MODE=access" in guide
    assert "must not be used for the public customer app" in pi_installer


def test_installer_preserves_persistent_runtime_backend() -> None:
    script = (ROOT / "scripts/install-pi5-led-service.sh").read_text(encoding="utf-8")

    assert "searchable_storage_led.runtime_config" in script
    assert '--installed "$target_config"' in script
    assert 'mv "$merged_config" "$target_config"' in script


def test_direct_deployer_preserves_persistent_runtime_backend() -> None:
    script = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")

    assert "searchable_storage_led.runtime_config" in script
    assert '--installed "$installed_config"' in script
    assert 'mv "$merged_config" "$installed_config"' in script


def _hosted_deploy_fixture(tmp_path: Path, *, fail_first_health: bool = False):
    config = tmp_path / "container-hosted.json"
    config.write_text(
        json.dumps(
            {
                "pixel_count": 0,
                "backend": "none",
                "storage": {
                    "backend": "supabase",
                    "supabase_url": "https://project.supabase.co",
                    "workspace_id": "",
                },
                "auth": {
                    "mode": "supabase",
                    "supabase_url": "https://project.supabase.co",
                    "workspace_id": "",
                    "multi_workspace": True,
                },
                "edge": {
                    "mode": "workspace",
                    "target_device_id": "",
                },
            }
        ),
        encoding="utf-8",
    )
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n", encoding="utf-8")
    log = tmp_path / "commands.log"
    health_calls = tmp_path / "health-calls"
    fake_bin = tmp_path / "bin"
    origin_check = fake_bin / "check-origin"
    _executable(
        fake_bin / "docker",
        f'''#!/bin/sh
printf '%s|%s|%s\n' "${{MAGPIE_IMAGE:-}}" "$*" "${{MAGPIE_CONFIG_FILE:-}}" >> {log}
if [ "$1" = inspect ]; then
  [ "${{FAIL_IMAGE_INSPECT:-false}}" != true ] || exit 1
  printf '%s\n' 'ghcr.io/example/magpie:sha-deadbee'
elif [ "$1" = cp ]; then
  [ "${{FAIL_RUNTIME_SNAPSHOT:-false}}" != true ] || exit 1
  case "$2" in
    *:/runtime/runtime.json)
      [ "${{FAIL_PRIMARY_RUNTIME_SNAPSHOT:-false}}" != true ] || exit 1
      ;;
  esac
  printf '%s\n' '{{"backend":"none","storage":{{"backend":"supabase"}}}}' > "$3"
elif echo "$*" | grep -q 'ps -q magpie'; then
  printf '%s\n' 'old-container'
elif echo "$*" | grep -q 'run --rm magpie-data-init' && [ "${{FAIL_DATA_INIT:-false}}" = true ]; then
  exit 1
elif echo "$*" | grep -q 'run --rm magpie-preflight' && [ "${{FAIL_PREFLIGHT:-false}}" = true ]; then
  exit 1
fi
''',
    )
    _executable(
        fake_bin / "curl",
        f'''#!/bin/sh
url=''
for arg in "$@"; do url="$arg"; done
if echo "$url" | grep -q '/ready$'; then
  calls=0
  [ -f {health_calls} ] && calls=$(cat {health_calls})
  calls=$((calls + 1))
  printf '%s' "$calls" > {health_calls}
  if [ "${{FAIL_FIRST_HEALTH:-false}}" = true ] && [ "$calls" -eq 1 ]; then
    exit 22
  fi
  if [ "${{FAKE_AGENT_READY:-true}}" = true ]; then
    agent='{{"ready":true,"effective_backend":"openrouter","provider":"openrouter","model":"test-model","api":"chat_completions"}}'
  else
    agent='{{"ready":false,"effective_backend":"deterministic","provider":"openrouter","model":"test-model","api":"chat_completions"}}'
  fi
  printf '%s\n' '{{"ok":true,"ready":true,"backend":"none","pixel_count":0,"agent":'"$agent"',"storage":{{"backend":"supabase","workspace_id":"request-scoped"}},"storage_probe":{{"ok":true,"backend":"supabase","workspace_id":"request-scoped"}},"operational_state_probe":{{"ok":true}},"auth":{{"mode":"supabase","enabled":true,"workspace_id":"request-scoped","multi_workspace":true}},"edge":{{"mode":"workspace","target_device_id":""}},"runtime":{{"schema":"magpie.runtime.v1","role":"central_service","hardware_owner":false,"storage_authority":"supabase","workspace_scope":"request","edge_execution":"workspace_queue","edge_queue_enabled":true}}}}'
elif echo "$url" | grep -q '/auth/config$'; then
  printf '{{"schema":"magpie.auth.config.v1","mode":"supabase","multi_workspace":true,"supabase_url":"https://project.supabase.co","publishable_key":"%s"}}\n' "${{FAKE_PUBLISHABLE_KEY:-sb_publishable_1234567890abcdefghij_12345678}}"
else
  printf '{{"commit":"%s","label":"v0.1.0 %s"}}\n' "${{FAKE_BUILD_COMMIT:-abcdef1234567890}}" "${{FAKE_BUILD_COMMIT:-abcdef1}}"
fi
''',
    )
    _executable(
        origin_check,
        f'''#!/bin/sh
printf 'origin|%s|%s|%s\\n' "${{MAGPIE_ORIGIN_MODE:-}}" "${{MAGPIE_PUBLIC_ORIGIN:-}}" "${{CF_ACCESS_CLIENT_ID:-}}" >> {log}
[ "${{FAIL_EXTERNAL_ORIGIN:-false}}" != true ]
''',
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DOCKER_BIN": str(fake_bin / "docker"),
        "CURL_BIN": str(fake_bin / "curl"),
        "MAGPIE_COMPOSE_FILE": str(compose),
        "MAGPIE_CONFIG_FILE": str(config),
        "MAGPIE_IMAGE": "ghcr.io/example/magpie:sha-abcdef1",
        "MAGPIE_VERIFY_ATTEMPTS": "1",
        "MAGPIE_VERIFY_INTERVAL_SECONDS": "0",
        "SUPABASE_PUBLISHABLE_KEY": TEST_SUPABASE_PUBLISHABLE_KEY,
        "SUPABASE_SERVICE_ROLE_KEY": "service-role",
        "OPENROUTER_API_KEY": "openrouter",
        "FAIL_FIRST_HEALTH": str(fail_first_health).lower(),
        "FAKE_BUILD_COMMIT": "abcdef1234567890",
        "MAGPIE_ORIGIN_CHECK_SCRIPT": str(origin_check),
    }
    return env, log


def test_hosted_deployer_promotes_verified_immutable_image(tmp_path) -> None:
    env, log = _hosted_deploy_fixture(tmp_path)

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    commands = log.read_text(encoding="utf-8")
    assert "pull magpie" in commands
    assert "--profile verify run --rm magpie-preflight" in commands
    assert "run --rm magpie-data-init" in commands
    assert commands.index("cp old-container:/runtime/runtime.json") < commands.index(
        "run --rm magpie-data-init"
    )
    assert "up -d --no-deps magpie" in commands
    assert commands.index("run --rm magpie-data-init") < commands.index(
        "--profile verify run --rm magpie-preflight"
    )
    assert commands.index("run --rm magpie-data-init") < commands.index(
        "up -d --no-deps magpie"
    )
    assert "deployed build: v0.1.0 abcdef1" in result.stdout


def test_hosted_deployer_rejects_invalid_publishable_key_before_docker(
    tmp_path,
) -> None:
    env, log = _hosted_deploy_fixture(tmp_path)
    invalid = "unrelated notification text"
    env["SUPABASE_PUBLISHABLE_KEY"] = invalid

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "must contain a Supabase publishable key or legacy anon key" in result.stderr
    assert invalid not in result.stderr
    assert not log.exists()


def test_hosted_deployer_rolls_back_invalid_public_auth_bootstrap(
    tmp_path,
) -> None:
    env, log = _hosted_deploy_fixture(tmp_path)
    env["FAKE_PUBLISHABLE_KEY"] = "not-a-client-key"

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "browser client key" in result.stderr
    assert "rolling back" in result.stderr
    commands = log.read_text(encoding="utf-8")
    assert "cp old-container:/runtime/runtime.json" in commands
    assert "ghcr.io/example/magpie:sha-deadbee|compose" in commands
    rollback_init = [
        line
        for line in commands.splitlines()
        if line.startswith("ghcr.io/example/magpie:sha-deadbee|")
        and "run --rm magpie-data-init" in line
    ]
    assert len(rollback_init) == 1
    assert rollback_init[0].endswith("previous-runtime.json")


def test_hosted_deployer_stops_when_previous_runtime_cannot_be_snapshotted(
    tmp_path,
) -> None:
    env, log = _hosted_deploy_fixture(tmp_path)
    env["FAIL_RUNTIME_SNAPSHOT"] = "true"

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "could not snapshot previous runtime config" in result.stderr
    commands = log.read_text(encoding="utf-8")
    assert "cp old-container:/runtime/runtime.json" in commands
    assert "run --rm magpie-data-init" not in commands
    assert "up -d --no-deps magpie" not in commands


def test_hosted_deployer_snapshots_legacy_runtime_before_promotion(tmp_path) -> None:
    env, log = _hosted_deploy_fixture(tmp_path)
    env["FAIL_PRIMARY_RUNTIME_SNAPSHOT"] = "true"

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    commands = log.read_text(encoding="utf-8")
    current_path = "cp old-container:/runtime/runtime.json"
    legacy_path = "cp old-container:/app/config/runtime.json"
    assert current_path in commands
    assert legacy_path in commands
    assert commands.index(current_path) < commands.index(legacy_path)
    assert commands.index(legacy_path) < commands.index("run --rm magpie-data-init")


def test_hosted_deployer_stops_when_previous_image_cannot_be_identified(
    tmp_path,
) -> None:
    env, log = _hosted_deploy_fixture(tmp_path)
    env["FAIL_IMAGE_INSPECT"] = "true"

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "could not identify the currently running image" in result.stderr
    commands = log.read_text(encoding="utf-8")
    assert "cp old-container:/runtime/runtime.json" not in commands
    assert "run --rm magpie-data-init" not in commands
    assert "up -d --no-deps magpie" not in commands


def test_hosted_deployer_stops_before_promotion_when_data_init_fails(
    tmp_path,
) -> None:
    env, log = _hosted_deploy_fixture(tmp_path)
    env["FAIL_DATA_INIT"] = "true"

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert (
        "runtime config and operational storage preparation failed"
        in result.stderr
    )
    commands = log.read_text(encoding="utf-8")
    assert "run --rm magpie-data-init" in commands
    assert "--profile verify run --rm magpie-preflight" not in commands
    assert "up -d --no-deps magpie" not in commands


def test_hosted_deployer_restores_runtime_when_supabase_preflight_fails(
    tmp_path,
) -> None:
    env, log = _hosted_deploy_fixture(tmp_path)
    env["FAIL_PREFLIGHT"] = "true"

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Supabase preflight failed; previous runtime config restored" in result.stderr
    commands = log.read_text(encoding="utf-8")
    assert "--profile verify run --rm magpie-preflight" in commands
    rollback_init = [
        line
        for line in commands.splitlines()
        if line.startswith("ghcr.io/example/magpie:sha-deadbee|")
        and "run --rm magpie-data-init" in line
    ]
    assert len(rollback_init) == 1
    assert rollback_init[0].endswith("previous-runtime.json")
    assert "up -d --no-deps magpie" not in commands


def test_hosted_deployer_uses_storage_readiness_gate() -> None:
    script = (ROOT / "scripts/deploy-hosted.sh").read_text(encoding="utf-8")

    assert '"$BASE_URL/ready"' in script
    assert '"$BASE_URL/health"' not in script
    assert '"storage probe": (health.get("storage_probe") or {}).get("ok") is True' in script
    assert '"operational state": (' in script
    assert 'health.get("operational_state_probe") or {}' in script


def test_hosted_deployer_rejects_fixed_workspace_config(tmp_path) -> None:
    env, log = _hosted_deploy_fixture(tmp_path)
    config_path = Path(env["MAGPIE_CONFIG_FILE"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["storage"]["workspace_id"] = "workspace-1"
    config["auth"]["workspace_id"] = "workspace-1"
    config["auth"]["multi_workspace"] = False
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "request-scoped multi-workspace auth" in result.stderr
    assert "workspace IDs must be request-scoped" in result.stderr
    assert not log.exists()


def test_hosted_deployer_can_require_machine_only_edge_origin(tmp_path) -> None:
    env, log = _hosted_deploy_fixture(tmp_path)
    env["MAGPIE_REQUIRE_EDGE_ORIGIN"] = "true"

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "requires an HTTPS machine-only edge origin" in result.stderr
    assert not log.exists()


def test_hosted_deployer_accepts_required_machine_only_edge_origin(tmp_path) -> None:
    env, _ = _hosted_deploy_fixture(tmp_path)
    env["MAGPIE_REQUIRE_EDGE_ORIGIN"] = "true"
    config_path = Path(env["MAGPIE_CONFIG_FILE"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["edge"]["machine_url"] = "https://edge.magpie.example"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_hosted_deployer_rolls_back_failed_external_origin(tmp_path) -> None:
    env, log = _hosted_deploy_fixture(tmp_path)
    env.update(
        {
            "MAGPIE_REQUIRE_EDGE_ORIGIN": "true",
            "MAGPIE_VERIFY_EXTERNAL_ORIGINS": "true",
            "MAGPIE_PUBLIC_ORIGIN": "https://magpie.example",
            "MAGPIE_EDGE_ORIGIN": "https://edge.magpie.example",
            "CF_ACCESS_CLIENT_ID": "machine-id",
            "CF_ACCESS_CLIENT_SECRET": "machine-secret",
            "FAIL_EXTERNAL_ORIGIN": "true",
        }
    )
    config_path = Path(env["MAGPIE_CONFIG_FILE"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["edge"]["machine_url"] = env["MAGPIE_EDGE_ORIGIN"]
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "failed external origin verification; rolling back" in result.stderr
    commands = log.read_text(encoding="utf-8")
    assert "origin|public|https://magpie.example|" in commands
    assert "machine-secret" not in commands
    assert "ghcr.io/example/magpie:sha-deadbee|compose" in commands


def test_hosted_deployer_verifies_public_and_edge_origins(tmp_path) -> None:
    env, log = _hosted_deploy_fixture(tmp_path)
    env.update(
        {
            "MAGPIE_REQUIRE_EDGE_ORIGIN": "true",
            "MAGPIE_VERIFY_EXTERNAL_ORIGINS": "true",
            "MAGPIE_PUBLIC_ORIGIN": "https://magpie.example",
            "MAGPIE_EDGE_ORIGIN": "https://edge.magpie.example",
            "CF_ACCESS_CLIENT_ID": "machine-id",
            "CF_ACCESS_CLIENT_SECRET": "machine-secret",
        }
    )
    config_path = Path(env["MAGPIE_CONFIG_FILE"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["edge"]["machine_url"] = env["MAGPIE_EDGE_ORIGIN"]
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    commands = log.read_text(encoding="utf-8")
    assert "origin|public|https://magpie.example|" in commands
    assert "origin|access|https://edge.magpie.example|machine-id" in commands
    assert "machine-secret" not in commands


def test_hosted_deployer_rejects_edge_url_that_is_not_an_origin(tmp_path) -> None:
    env, log = _hosted_deploy_fixture(tmp_path)
    env["MAGPIE_REQUIRE_EDGE_ORIGIN"] = "true"
    config_path = Path(env["MAGPIE_CONFIG_FILE"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["edge"]["machine_url"] = "https://edge.magpie.example/private"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "requires an HTTPS machine-only edge origin" in result.stderr
    assert not log.exists()


def test_hosted_deployer_rejects_mutable_image(tmp_path) -> None:
    env, log = _hosted_deploy_fixture(tmp_path)
    env["MAGPIE_IMAGE"] = "ghcr.io/example/magpie:main"

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "immutable sha-* tag or sha256 digest" in result.stderr
    assert not log.exists()


def test_hosted_deployer_requires_at_least_one_agent_provider(tmp_path) -> None:
    env, log = _hosted_deploy_fixture(tmp_path)
    env.pop("OPENROUTER_API_KEY")
    env.pop("OPENAI_API_KEY", None)

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "OpenAI, OpenRouter, or Cloudflare AI credential is required" in result.stderr
    assert not log.exists()


def test_hosted_deployer_requires_cloudflare_account_with_token(tmp_path) -> None:
    env, log = _hosted_deploy_fixture(tmp_path)
    env.pop("OPENROUTER_API_KEY")
    env.pop("OPENAI_API_KEY", None)
    env["CLOUDFLARE_AI_API_TOKEN"] = "workers-ai-token"

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "CLOUDFLARE_ACCOUNT_ID is required" in result.stderr
    assert not log.exists()


def test_hosted_deployer_rolls_back_failed_runtime(tmp_path) -> None:
    env, log = _hosted_deploy_fixture(tmp_path, fail_first_health=True)

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "rolling back" in result.stderr
    commands = log.read_text(encoding="utf-8")
    assert "ghcr.io/example/magpie:sha-abcdef1|compose" in commands
    assert "ghcr.io/example/magpie:sha-deadbee|compose" in commands
    assert "restored ghcr.io/example/magpie:sha-deadbee" in result.stderr


def test_hosted_deployer_rolls_back_unavailable_agent(tmp_path) -> None:
    env, log = _hosted_deploy_fixture(tmp_path)
    env["FAKE_AGENT_READY"] = "false"

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "configured storage agent" in result.stderr
    assert "non-deterministic agent backend" in result.stderr
    assert "rolling back" in result.stderr
    assert "ghcr.io/example/magpie:sha-deadbee|compose" in log.read_text(
        encoding="utf-8"
    )


def test_hosted_deployer_rolls_back_unexpected_build(tmp_path) -> None:
    env, log = _hosted_deploy_fixture(tmp_path)
    env["FAKE_BUILD_COMMIT"] = "9999999999999999"

    result = subprocess.run(
        [str(ROOT / "scripts/deploy-hosted.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "expected commit abcdef1" in result.stderr
    assert "rolling back" in result.stderr
    assert "ghcr.io/example/magpie:sha-deadbee|compose" in log.read_text(
        encoding="utf-8"
    )






def test_hosted_verifier_pins_compatible_supabase_cli() -> None:
    verifier = _load_hosted_verifier()
    source = Path(verifier.__file__).read_text(encoding="utf-8")

    assert verifier.SUPABASE_CLI_VERSION == "2.109.1"
    assert verifier.SUPABASE == ["npx", "--yes", "supabase@2.109.1"]
    assert verifier.EXCLUDED_SUPABASE_SERVICES == (
        "studio,imgproxy,edge-runtime,realtime,logflare,vector,mailpit"
    )
    start_marker = 'f"Starting isolated Supabase at deployed migration "'
    attempt_marker = "started_supabase = True"
    assert source.index(start_marker) < source.index(
        attempt_marker, source.index(start_marker)
    )
    assert 'supabase_command(supabase_workdir, "stop", "--no-backup")' in source


def test_hosted_verifier_reports_bounded_command_timeout(monkeypatch) -> None:
    verifier = _load_hosted_verifier()
    captured: dict[str, object] = {}

    def timeout_run(command, **kwargs):
        captured["command"] = command
        captured["timeout"] = kwargs["timeout"]
        raise subprocess.TimeoutExpired(
            command,
            kwargs["timeout"],
            output=b"starting database\nwaiting for volume\n",
        )

    monkeypatch.setattr(verifier.subprocess, "run", timeout_run)

    try:
        verifier.run(["npx", "supabase", "start"], timeout_seconds=12)
    except verifier.SmokeFailure as exc:
        message = str(exc)
    else:
        raise AssertionError("expected command timeout to fail the hosted verifier")

    assert captured == {
        "command": ["npx", "supabase", "start"],
        "timeout": 12,
    }
    assert "command timed out after 12s: npx supabase start" in message
    assert "starting database\nwaiting for volume" in message


def test_hosted_verifier_disables_supabase_cli_telemetry(monkeypatch) -> None:
    verifier = _load_hosted_verifier()
    captured: dict[str, str] = {}

    def capture_run(_command, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(_command, 0, "", "")

    monkeypatch.setattr(verifier.subprocess, "run", capture_run)

    verifier.run(["npx", "supabase", "status"])

    assert captured["SUPABASE_TELEMETRY"] == "false"
    assert captured["SUPABASE_TELEMETRY_DISABLED"] == "1"
    assert captured["DO_NOT_TRACK"] == "1"


def test_hosted_verifier_cleanup_timeout_is_best_effort(monkeypatch, capsys) -> None:
    verifier = _load_hosted_verifier()

    def timeout_run(*_args, **_kwargs):
        raise verifier.SmokeFailure("cleanup timed out")

    monkeypatch.setattr(verifier, "run", timeout_run)

    verifier.run_cleanup(["docker", "rm", "-f", "smoke"])

    assert "cleanup warning: cleanup timed out" in capsys.readouterr().err


def test_hosted_verifier_prepares_and_promotes_upgrade_migrations(
    tmp_path, monkeypatch
) -> None:
    verifier = _load_hosted_verifier()
    source = tmp_path / "source"
    migrations = source / "migrations"
    migrations.mkdir(parents=True)
    (source / "config.toml").write_text(
        '\n'.join(
            [
                'project_id = "source-project"',
                "[api]",
                "port = 54321",
                "[db]",
                "port = 54322",
                "shadow_port = 54320",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    baseline = "20260728143000"
    before = migrations / "20260728090000_before.sql"
    boundary = migrations / f"{baseline}_boundary.sql"
    pending_one = migrations / "20260728173000_pending.sql"
    pending_two = migrations / "20260801050000_latest.sql"
    for migration in (before, boundary, pending_one, pending_two):
        migration.write_text(f"-- {migration.name}\n", encoding="utf-8")
    monkeypatch.setattr(verifier, "_free_local_ports", lambda count: [6101, 6102, 6103])

    workdir = tmp_path / "workdir"
    pending = verifier.prepare_upgrade_project(
        workdir,
        source=source,
        baseline=baseline,
    )

    staged = workdir / "supabase" / "migrations"
    assert sorted(path.name for path in staged.iterdir()) == sorted(
        [before.name, boundary.name]
    )
    assert pending == [pending_one, pending_two]
    config = (workdir / "supabase" / "config.toml").read_text(encoding="utf-8")
    assert 'project_id = "source-project"' not in config
    assert "port = 6101" in config
    assert "port = 6102" in config
    assert "shadow_port = 6103" in config

    verifier.promote_upgrade_migrations(workdir, pending)

    assert sorted(path.name for path in staged.iterdir()) == sorted(
        migration.name for migration in (before, boundary, pending_one, pending_two)
    )


def test_hosted_verifier_rejects_missing_upgrade_boundary(tmp_path) -> None:
    verifier = _load_hosted_verifier()
    source = tmp_path / "source"
    migrations = source / "migrations"
    migrations.mkdir(parents=True)
    (source / "config.toml").write_text(
        'project_id = "source"\n[api]\nport = 54321\n'
        '[db]\nport = 54322\nshadow_port = 54320\n',
        encoding="utf-8",
    )
    (migrations / "20260801050000_only.sql").write_text("-- latest\n", encoding="utf-8")

    with pytest.raises(verifier.SmokeFailure, match="baseline migration .* was not found"):
        verifier.prepare_upgrade_project(
            tmp_path / "workdir",
            source=source,
            baseline="20260728143000",
        )


def test_hosted_verifier_waits_for_postgrest_schema_reload(monkeypatch) -> None:
    verifier = _load_hosted_verifier()
    attempts = 0

    def fake_rest_rows(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise verifier.SmokeFailure(
                "POST /active_guidance_states returned 404: PGRST205"
            )
        return [{"workspace_id": "workspace"}]

    monkeypatch.setattr(verifier, "_rest_rows", fake_rest_rows)
    monkeypatch.setattr(verifier.time, "sleep", lambda _seconds: None)

    assert verifier._rest_rows_after_schema_reload(
        "POST",
        "/active_guidance_states",
        service_key="secret",
    ) == [{"workspace_id": "workspace"}]
    assert attempts == 3


def test_hosted_verifier_derives_exact_identity_from_clean_checkout(
    monkeypatch,
) -> None:
    verifier = _load_hosted_verifier()
    commit = "a" * 40
    outputs = {
        ("git", "status", "--porcelain=v1", "--untracked-files=all"): "",
        ("git", "rev-parse", "HEAD"): f"{commit}\n",
        ("git", "branch", "--show-current"): "feature/release\n",
        ("git", "show", "-s", "--format=%cI", "HEAD"): (
            "2026-07-30T12:00:00-04:00\n"
        ),
    }

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, outputs[tuple(command)], "")

    monkeypatch.setattr(verifier, "run", fake_run)

    assert verifier.release_identity() == {
        "commit": commit,
        "branch": "feature/release",
        "built_at": "2026-07-30T12:00:00-04:00",
    }


def test_hosted_verifier_rejects_dirty_release_source(monkeypatch) -> None:
    verifier = _load_hosted_verifier()

    monkeypatch.setattr(
        verifier,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            " M src/searchable_storage_led/api.py\n?? local-note.txt\n",
            "",
        ),
    )

    try:
        verifier.release_identity()
    except verifier.SmokeFailure as exc:
        message = str(exc)
    else:
        raise AssertionError("expected dirty release source to be rejected")

    assert "requires a clean checkout" in message
    assert "2 changed or untracked path(s)" in message


def test_hosted_verifier_requires_running_container_to_match_candidate(
    monkeypatch,
) -> None:
    verifier = _load_hosted_verifier()
    expected = {
        "commit": "a" * 40,
        "branch": "feature/release",
        "built_at": "2026-07-30T12:00:00-04:00",
    }

    monkeypatch.setattr(
        verifier,
        "request_json",
        lambda *_args, **_kwargs: (
            200,
            {
                "commit": expected["commit"],
                "dirty": False,
                "source": "container",
            },
        ),
    )
    assert verifier.verify_build_identity("http://magpie.test", expected)["commit"] == (
        expected["commit"]
    )

    monkeypatch.setattr(
        verifier,
        "request_json",
        lambda *_args, **_kwargs: (
            200,
            {"commit": "b" * 40, "dirty": False, "source": "container"},
        ),
    )
    try:
        verifier.verify_build_identity("http://magpie.test", expected)
    except verifier.SmokeFailure as exc:
        message = str(exc)
    else:
        raise AssertionError("expected mismatched image identity to be rejected")

    assert "does not match the candidate" in message
    assert expected["commit"] in message


def test_hosted_verifier_preflights_actual_docker_container_execution(
    monkeypatch,
) -> None:
    verifier = _load_hosted_verifier()
    calls: list[tuple[list[str], bool, float | None]] = []
    cleanup: list[list[str]] = []

    def fake_run(
        command,
        *,
        check=True,
        environment=None,
        timeout_seconds=verifier.DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ):
        del environment
        calls.append((command, check, timeout_seconds))
        return subprocess.CompletedProcess(command, 0, "ok\n", "")

    monkeypatch.setattr(verifier, "run", fake_run)
    monkeypatch.setattr(verifier, "run_cleanup", cleanup.append)

    verifier.verify_docker_runtime()

    container_name = f"magpie-docker-preflight-{os.getpid()}"
    assert calls == [
        (
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            True,
            verifier.DOCKER_INFO_TIMEOUT_SECONDS,
        ),
        (
            ["docker", "image", "inspect", verifier.DOCKER_PREFLIGHT_IMAGE],
            False,
            verifier.DOCKER_INFO_TIMEOUT_SECONDS,
        ),
        (
            [
                "docker",
                "run",
                "--name",
                container_name,
                verifier.DOCKER_PREFLIGHT_IMAGE,
            ],
            True,
            verifier.DOCKER_RUN_TIMEOUT_SECONDS,
        ),
    ]
    assert cleanup == [["docker", "rm", "-f", container_name]]


def test_hosted_verifier_diagnoses_created_but_unstarted_container(
    monkeypatch,
) -> None:
    verifier = _load_hosted_verifier()
    cleanup: list[list[str]] = []

    def fake_run(
        command,
        *,
        check=True,
        environment=None,
        timeout_seconds=verifier.DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ):
        del check, environment, timeout_seconds
        if command[:2] == ["docker", "run"]:
            raise verifier.SmokeFailure("command timed out after 20s")
        if command[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(command, 0, "created \n", "")
        return subprocess.CompletedProcess(command, 0, "ok\n", "")

    monkeypatch.setattr(verifier, "run", fake_run)
    monkeypatch.setattr(verifier, "run_cleanup", cleanup.append)

    try:
        verifier.verify_docker_runtime()
    except verifier.SmokeFailure as exc:
        message = str(exc)
    else:
        raise AssertionError("expected the Docker runtime preflight to fail")

    assert "Docker created the preflight container but could not start it" in message
    assert "Restart or repair the Docker Desktop runtime/VM" in message
    assert "image pulls and docker info do not prove containers can run" in message
    assert cleanup == [
        ["docker", "rm", "-f", f"magpie-docker-preflight-{os.getpid()}"]
    ]


def test_hosted_verifier_can_stop_after_container_runtime_preflight(
    monkeypatch,
    capsys,
) -> None:
    verifier = _load_hosted_verifier()
    calls: list[str] = []
    cleanup: list[list[str]] = []

    monkeypatch.setattr(
        verifier,
        "verify_docker_runtime",
        lambda: calls.append("docker"),
    )
    monkeypatch.setattr(
        verifier,
        "supabase_environment",
        lambda: calls.append("supabase") or {},
    )
    monkeypatch.setattr(verifier, "run_cleanup", cleanup.append)
    monkeypatch.setattr(
        sys,
        "argv",
        ["verify_hosted_local.py", "--docker-preflight-only"],
    )

    assert verifier.main() == 0
    assert calls == ["docker"]
    assert "Docker container execution is ready." in capsys.readouterr().out
    assert cleanup == [
        ["docker", "rm", "-f", f"magpie-hosted-smoke-{os.getpid()}"]
    ]


def test_hosted_verifier_builds_and_checks_exact_candidate_identity() -> None:
    source = (ROOT / "scripts/verify_hosted_local.py").read_text(encoding="utf-8")

    assert '["git", "status", "--porcelain=v1", "--untracked-files=all"]' in source
    assert 'f"BUILD_COMMIT={identity[\'commit\']}"' in source
    assert 'f"BUILD_BRANCH={identity[\'branch\']}"' in source
    assert 'f"BUILT_AT={identity[\'built_at\']}"' in source
    assert "build = verify_build_identity(base_url, identity)" in source
    assert source.index("wait_for_health(base_url, container_name)") < source.index(
        "verify_build_identity(base_url, identity)"
    )
    identity_check = source.index("verify_build_identity(base_url, identity)")
    assert identity_check < source.index("exercise_tenants(", identity_check)


def test_hosted_verifier_enforces_visual_only_storage_setup() -> None:
    source = (ROOT / "scripts/verify_hosted_local.py").read_text(encoding="utf-8")

    voice_setup = source.index('"source": "voice"')
    ui_setup = source.index("garage_setup = ui_storage_setup(", voice_setup)

    assert voice_setup < ui_setup
    assert '"place_configuration_ui_required"' in source[voice_setup:ui_setup]
    assert '"/storage?view=places"' in source[voice_setup:ui_setup]
    assert "after_voice_setup_ids != before_voice_setup_ids" in source[
        voice_setup:ui_setup
    ]


def test_hosted_verifier_exercises_portable_workspace_restore() -> None:
    source = (ROOT / "scripts/verify_hosted_local.py").read_text(encoding="utf-8")

    assert "def exercise_portable_workspace_restore(" in source
    assert 'f"{base_url}/export/inventory.json"' in source
    assert 'payload={"snapshot": snapshot, "dry_run": True}' in source
    assert 'payload={"snapshot": snapshot}' in source
    assert 'restore_workspace_name = f"Hosted Restore {nonce}"' in source
    assert 'payload={"name": restore_workspace_name}' in source
    assert "exercise_portable_workspace_restore(" in source
    assert '"photos": ("filename", "content_type", "data_base64")' in source


def test_portable_workspace_restore_previews_imports_and_compares_data() -> None:
    verifier = _load_hosted_verifier()
    snapshot = {
        "schema": "magpie.inventory.v1",
        "locations": [
            {
                "id": "garage",
                "parent_id": None,
                "type": "room",
                "label": "Garage",
                "active": True,
                "photo_ids": [7],
            }
        ],
        "inventory": [
            {
                "name": "Caliper",
                "bin_id": "garage",
                "quantity": 1,
                "unit": "each",
                "active": True,
                "needs_review": False,
                "photo_ids": [7],
            }
        ],
        "placement_rules": [
            {
                "rule_text": "Calipers go in Garage.",
                "trigger_terms": ["caliper"],
                "target_location_id": "garage",
                "active": True,
            }
        ],
        "size_rules": [
            {
                "rule_text": "Calipers are medium.",
                "trigger_terms": ["caliper"],
                "size_class": "medium",
                "active": True,
            }
        ],
        "photos": [
            {
                "id": 7,
                "filename": "caliper.jpg",
                "content_type": "image/jpeg",
                "data_base64": "aW1hZ2U=",
            }
        ],
    }
    calls: list[dict[str, object]] = []

    def request(method, url, *, payload=None, headers=None, expected={200}):
        del expected
        calls.append(
            {"method": method, "url": url, "payload": payload, "headers": headers}
        )
        if method == "GET":
            return 200, snapshot
        counts = {
            key: len(snapshot[key])
            for key in (
                "locations",
                "inventory",
                "placement_rules",
                "size_rules",
                "photos",
            )
        }
        if payload.get("dry_run") is True:
            return 200, {"dry_run": True, "imported": 0, **counts}
        return 200, {"dry_run": False, "imported": 1, **counts}

    verifier.exercise_portable_workspace_restore(
        "https://magpie.example",
        {"X-Magpie-Workspace": "source"},
        {"X-Magpie-Workspace": "restore"},
        request_fn=request,
    )

    assert [call["method"] for call in calls] == ["GET", "POST", "POST", "GET"]
    assert calls[1]["payload"] == {"snapshot": snapshot, "dry_run": True}
    assert calls[2]["payload"] == {"snapshot": snapshot}
    assert calls[0]["headers"] == {"X-Magpie-Workspace": "source"}
    assert calls[3]["headers"] == {"X-Magpie-Workspace": "restore"}








def test_hosted_journey_exercises_workspace_scoped_edge_controller() -> None:
    verifier = _load_hosted_verifier()
    calls: list[dict[str, object]] = []
    device_id = "smoke-cabinet-test"
    base_url = "https://magpie.example"

    def request(
        method: str,
        url: str,
        *,
        payload: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        expected: set[int] = {200},
    ) -> tuple[int, dict[str, object]]:
        calls.append(
            {
                "method": method,
                "url": url,
                "payload": payload,
                "headers": headers,
                "expected": expected,
            }
        )
        if url.endswith("/workspace/devices/pairing"):
            return 200, {
                "pairing_code": "one-use-pairing-code",
                "shown_once": True,
                "machine_url": "https://edge.magpie.example",
            }
        if url.endswith(f"/edge/devices/{device_id}/pair"):
            if expected == {401}:
                return 401, {"detail": "invalid or expired pairing code"}
            return 200, {"token": "device-token", "shown_once": True}
        if url.endswith(f"/edge/devices/{device_id}/heartbeat"):
            if expected == {401}:
                return 401, {"detail": "invalid edge device credentials"}
            return 200, {"ok": True}
        if url.endswith("/locations/tool-box/update"):
            return 200, {"ok": True}
        if method == "GET" and url.endswith("/workspace/devices"):
            return 200, {
                "devices": [
                    {
                        "device_id": device_id,
                        "command_ready": True,
                        "bound_locations": [{"id": "tool-box", "label": "Tool Box"}],
                    }
                ]
            }
        if url.endswith("/locations/tool-box/light"):
            return 200, {"lit": True}
        if url.endswith(f"/edge/devices/{device_id}/commands/claim"):
            if expected == {401}:
                return 401, {"detail": "invalid edge device credentials"}
            return 200, {
                "command": {
                    "id": "command-1",
                    "kind": "set_pixels",
                    "payload": {"pixel_count": 1, "pixels": [[255, 255, 255]]},
                }
            }
        if url.endswith(
            f"/edge/devices/{device_id}/commands/command-1/complete"
        ):
            return 200, {"command": {"id": "command-1", "status": "succeeded"}}
        if method == "DELETE" and url.endswith(f"/workspace/devices/{device_id}"):
            return 200, {"ok": True}
        raise AssertionError(f"unexpected hosted verifier request: {method} {url}")

    verifier.exercise_edge_controller(
        base_url,
        {
            "Authorization": "Bearer owner-token",
            "X-Magpie-Workspace": "workspace-one",
        },
        workspace_id="workspace-one",
        other_workspace_id="workspace-two",
        location_id="tool-box",
        nonce="test",
        request_fn=request,
    )

    assert any(
        call["url"].endswith(f"/edge/devices/{device_id}/commands/claim")
        and call["headers"]["X-Magpie-Workspace"] == "workspace-two"
        and call["expected"] == {401}
        for call in calls
    )
    assert any(
        call["method"] == "DELETE"
        and call["url"].endswith(f"/workspace/devices/{device_id}")
        for call in calls
    )


def test_hosted_config_renderer_creates_protected_multi_workspace_config(
    tmp_path,
) -> None:
    output = tmp_path / "container-hosted.json"
    result = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts/render-hosted-config.py"),
            "--template",
            str(ROOT / "config/container-supabase.example.json"),
            "--output",
            str(output),
            "--supabase-url",
            "https://project.supabase.co/",
            "--public-origin",
            "https://magpie.example/",
            "--edge-origin",
            "https://edge.magpie.example/",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    config = json.loads(output.read_text(encoding="utf-8"))
    assert config["backend"] == "none"
    assert config["pixel_count"] == 0
    assert config["storage"]["supabase_url"] == "https://project.supabase.co"
    assert config["auth"]["supabase_url"] == "https://project.supabase.co"
    assert config["auth"]["multi_workspace"] is True
    assert config["public_origin"] == "https://magpie.example"
    assert config["zebra"]["base_url"] == "https://magpie.example"
    assert config["edge"]["machine_url"] == "https://edge.magpie.example"
    assert output.stat().st_mode & 0o777 == 0o600


def test_hosted_config_renderer_rejects_non_https_origin(tmp_path) -> None:
    result = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts/render-hosted-config.py"),
            "--template",
            str(ROOT / "config/container-supabase.example.json"),
            "--output",
            str(tmp_path / "invalid.json"),
            "--supabase-url",
            "http://project.supabase.co/path",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "HTTPS origin without a path" in result.stderr


def _hosted_prepare_env() -> dict[str, str]:
    return {
        **os.environ,
        "MAGPIE_SUPABASE_URL": "https://project.supabase.co",
        "MAGPIE_PUBLIC_ORIGIN": "https://magpie.example",
        "MAGPIE_EDGE_ORIGIN": "https://edge.magpie.example",
        "MAGPIE_IMAGE": "ghcr.io/example/magpie:sha-abcdef1",
        "SUPABASE_PUBLISHABLE_KEY": TEST_SUPABASE_PUBLISHABLE_KEY,
        "SUPABASE_SERVICE_ROLE_KEY": "service-role",
        "OPENROUTER_API_KEY": "agent-provider",
    }


def _load_prepare_hosted():
    spec = importlib.util.spec_from_file_location(
        "prepare_hosted",
        ROOT / "scripts/prepare-hosted.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hosted_prepare_renders_valid_config_without_starting_containers(
    tmp_path,
) -> None:
    output = tmp_path / "container-hosted.json"
    result = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts/prepare-hosted.py"),
            "--output",
            str(output),
        ],
        env=_hosted_prepare_env(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    config = json.loads(output.read_text(encoding="utf-8"))
    assert config["storage"]["supabase_url"] == "https://project.supabase.co"
    assert config["auth"]["multi_workspace"] is True
    assert config["public_origin"] == "https://magpie.example"
    assert config["edge"]["machine_url"] == "https://edge.magpie.example"
    assert output.stat().st_mode & 0o777 == 0o600
    assert "No containers were started or changed." in result.stdout


def test_hosted_prepare_verifies_existing_workspace_before_replacing_config(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    module = _load_prepare_hosted()
    output = tmp_path / "container-hosted.json"
    workspace_id = "00000000-0000-4000-8000-000000000000"
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        if "render-hosted-config.py" in command[1]:
            render_output = Path(command[command.index("--output") + 1])
            render_output.parent.mkdir(parents=True, exist_ok=True)
            render_output.write_text('{"verified": true}\n', encoding="utf-8")
            render_output.chmod(0o600)
        return subprocess.CompletedProcess(command, 0, stdout="ready\n", stderr="")

    for name, value in _hosted_prepare_env().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare-hosted.py",
            "--output",
            str(output),
            "--existing-workspace-id",
            workspace_id,
        ],
    )

    assert module.main() == 0
    assert json.loads(output.read_text(encoding="utf-8")) == {"verified": True}
    assert output.stat().st_mode & 0o777 == 0o600
    assert not output.with_suffix(
        output.suffix + ".attachment-check.tmp"
    ).exists()
    assert len(calls) == 2
    assert calls[1][1:3] == ["-m", "searchable_storage_led.supabase_check"]
    assert "--verify-attachment" in calls[1]
    assert calls[1][calls[1].index("--workspace-id") + 1] == workspace_id
    assert "Existing workspace attachment verified" in capsys.readouterr().out


def test_hosted_prepare_attachment_failure_preserves_existing_config_and_secrets(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    module = _load_prepare_hosted()
    output = tmp_path / "container-hosted.json"
    output.write_text('{"previous": true}\n', encoding="utf-8")
    service_key = "service-role-must-not-leak"

    def run(command, **kwargs):
        if "render-hosted-config.py" in command[1]:
            render_output = Path(command[command.index("--output") + 1])
            render_output.write_text('{"candidate": true}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr=f"attachment denied for {service_key}",
        )

    environment = _hosted_prepare_env()
    environment["SUPABASE_SERVICE_ROLE_KEY"] = service_key
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare-hosted.py",
            "--output",
            str(output),
            "--existing-workspace-id",
            "00000000-0000-4000-8000-000000000000",
        ],
    )

    assert module.main() == 1
    assert json.loads(output.read_text(encoding="utf-8")) == {"previous": True}
    assert not output.with_suffix(
        output.suffix + ".attachment-check.tmp"
    ).exists()
    stderr = capsys.readouterr().err
    assert service_key not in stderr
    assert "[redacted]" in stderr


def test_hosted_prepare_rejects_invalid_existing_workspace_before_render(
    tmp_path,
) -> None:
    output = tmp_path / "container-hosted.json"
    result = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts/prepare-hosted.py"),
            "--output",
            str(output),
            "--existing-workspace-id",
            "main-cabinet",
        ],
        env=_hosted_prepare_env(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "must be a UUID" in result.stderr
    assert not output.exists()


def test_hosted_prepare_rejects_invalid_publishable_key_without_echoing_it(
    tmp_path,
) -> None:
    output = tmp_path / "container-hosted.json"
    environment = _hosted_prepare_env()
    invalid = "unexpected notification text"
    environment["SUPABASE_PUBLISHABLE_KEY"] = invalid

    result = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts/prepare-hosted.py"),
            "--output",
            str(output),
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "must contain a Supabase publishable key or legacy anon key" in result.stderr
    assert invalid not in result.stderr
    assert not output.exists()


def test_hosted_prepare_reports_all_missing_runtime_requirements(tmp_path) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "MAGPIE_SUPABASE_URL",
            "MAGPIE_PUBLIC_ORIGIN",
            "MAGPIE_IMAGE",
            "SUPABASE_PUBLISHABLE_KEY",
            "SUPABASE_SERVICE_ROLE_KEY",
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "CLOUDFLARE_AI_API_TOKEN",
        }
    }
    result = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts/prepare-hosted.py"),
            "--output",
            str(tmp_path / "container-hosted.json"),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "MAGPIE_SUPABASE_URL" in result.stderr
    assert "MAGPIE_PUBLIC_ORIGIN" in result.stderr
    assert "MAGPIE_IMAGE" in result.stderr
    assert "SUPABASE_PUBLISHABLE_KEY" in result.stderr
    assert "SUPABASE_SERVICE_ROLE_KEY" in result.stderr
    assert "one of OPENAI_API_KEY" in result.stderr
    assert not (tmp_path / "container-hosted.json").exists()


def test_hosted_prepare_rejects_mutable_image_before_rendering(tmp_path) -> None:
    env = _hosted_prepare_env()
    env["MAGPIE_IMAGE"] = "ghcr.io/example/magpie:main"
    output = tmp_path / "container-hosted.json"

    result = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts/prepare-hosted.py"),
            "--output",
            str(output),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "immutable sha-* tag or sha256 digest" in result.stderr
    assert not output.exists()














def test_kiosk_installer_uses_dedicated_local_display_or_secure_screen_bridge() -> None:
    installer = (ROOT / "scripts/install-pi5-kiosk.sh").read_text(encoding="utf-8")

    assert "http://127.0.0.1:8765/kiosk" in installer
    assert "MAGPIE_SCREEN_API_URL" in installer
    assert "EnvironmentFile=$ENV_PATH" in installer
    assert "Environment=MAGPIE_EDGE_DEVICE_ID=$EDGE_DEVICE_ID" in installer
    assert "Environment=MAGPIE_EDGE_WORKSPACE_ID=$EDGE_WORKSPACE_ID" in installer
    assert "Environment=MAGPIE_SCREEN_BIND_HOST=127.0.0.1" in installer
    assert "searchable_storage_led.screen_bridge" in installer
    assert "http://127.0.0.1:$SCREEN_PORT/kiosk" in installer
    assert "MAGPIE_EDGE_TOKEN" not in next(
        line for line in installer.splitlines() if line.startswith("ExecStart=")
    )


def test_edge_installer_persists_workspace_selector() -> None:
    installer = (ROOT / "scripts/install-pi5-edge-service.sh").read_text(
        encoding="utf-8"
    )

    assert 'WORKSPACE_ID="${MAGPIE_EDGE_WORKSPACE_ID:-}"' in installer
    assert "Environment=MAGPIE_EDGE_WORKSPACE_ID=$WORKSPACE_ID" in installer


def test_pi_installer_packages_cabinet_edge_companions() -> None:
    installer = (ROOT / "scripts/install-pi5-led-service.sh").read_text(
        encoding="utf-8"
    )

    assert 'mkdir -p "$APP_DIR/scripts"' in installer
    for script_name in (
        "connect-pi5-cabinet.sh",
        "install-pi5-edge-service.sh",
        "install-pi5-kiosk.sh",
        "install-pi5-voice-service.sh",
    ):
        assert script_name in installer
    assert 'chmod 755 "$APP_DIR/scripts/$companion_script"' in installer


def test_cabinet_connector_requires_authoritative_central_edge_status() -> None:
    connector = (ROOT / "scripts/connect-pi5-cabinet.sh").read_text(
        encoding="utf-8"
    )

    assert "/edge/devices/{device_id}/status" in connector
    assert 'latest.get("command_ready")' in connector
    assert "central edge verified:" in connector
    assert "did not report" in connector
    assert "http://127.0.0.1:8767/bridge/ready?require_voice=true" in connector
    assert "central screen and voice readiness was not verified" in connector


def _cabinet_activation_fixture(
    tmp_path: Path,
    *,
    kiosk_exit: int = 0,
    bridge_ready: bool = True,
) -> tuple[Path, Path, Path, dict[str, str]]:
    app = tmp_path / "app"
    home = tmp_path / "home"
    units = home / ".config/systemd/user"
    log = tmp_path / "commands.log"
    (app / ".venv/bin").mkdir(parents=True)
    (app / "scripts").mkdir()
    units.mkdir(parents=True)
    (app / "secrets.env").write_text(
        "MAGPIE_EDGE_TOKEN=abcdefghijklmnopqrstuvwxyz_123456\n",
        encoding="utf-8",
    )
    (units / "searchable-storage-voice.service").write_text(
        "local voice\n",
        encoding="utf-8",
    )
    (units / "searchable-storage-kiosk.service").write_text(
        "local kiosk\n",
        encoding="utf-8",
    )
    (units / "searchable-storage-led.service").write_text(
        "local api\n",
        encoding="utf-8",
    )
    (units / "searchable-storage-edge.service").write_text(
        "central edge worker\n",
        encoding="utf-8",
    )
    _executable(app / ".venv/bin/searchable-storage-edge", "#!/bin/sh\nexit 0\n")
    _executable(
        app / "scripts/install-pi5-edge-service.sh",
        f"#!/bin/sh\nprintf 'edge installed\\n' >> {log}\n",
    )
    _executable(
        app / "scripts/install-pi5-voice-service.sh",
        (
            "#!/bin/sh\n"
            f"printf 'central voice\\n' > {units / 'searchable-storage-voice.service'}\n"
            f"printf 'voice installed\\n' >> {log}\n"
        ),
    )
    _executable(
        app / "scripts/install-pi5-kiosk.sh",
        (
            "#!/bin/sh\n"
            f"printf 'central kiosk\\n' > {units / 'searchable-storage-kiosk.service'}\n"
            f"printf 'central screen\\n' > {units / 'searchable-storage-screen.service'}\n"
            f"printf 'kiosk installed\\n' >> {log}\n"
            f"exit {kiosk_exit}\n"
        ),
    )
    fake_systemctl = tmp_path / "systemctl"
    _executable(
        fake_systemctl,
        (
            "#!/bin/sh\n"
            f"printf 'systemctl %s\\n' \"$*\" >> {log}\n"
            "command_name=\"$2\"\n"
            "if [ \"$command_name\" = is-active ] || [ \"$command_name\" = is-enabled ]; then\n"
            "  service_name=\"${4:-${3:-}}\"\n"
            "  test -f \"$SYSTEMD_USER_DIR/$service_name\"\n"
            "  exit $?\n"
            "fi\n"
            "exit 0\n"
        ),
    )
    fake_curl = tmp_path / "curl"
    _executable(
        fake_curl,
        (
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *bridge/health*) printf '%s\\n' '{\"ok\":true}' ;;\n"
            + (
                "  *bridge/ready*) printf '%s\\n' "
                "'{\"ok\":true,\"central_ready\":true}' ;;\n"
                if bridge_ready
                else "  *bridge/ready*) exit 22 ;;\n"
            )
            + "  *) exit 22 ;;\n"
            "esac\n"
        ),
    )
    environment = {
        **os.environ,
        "HOME": str(home),
        "PYTHON_BIN": sys.executable,
        "SYSTEMCTL_BIN": str(fake_systemctl),
        "SYSTEMD_USER_DIR": str(units),
        "CURL_BIN": str(fake_curl),
        "MAGPIE_SKIP_CENTRAL_PREFLIGHT": "1",
        "MAGPIE_BRIDGE_HEALTH_ATTEMPTS": "1",
        "MAGPIE_BRIDGE_HEALTH_SLEEP_SECONDS": "0",
    }
    return app, units, log, environment


def test_cabinet_connector_pairs_edge_without_replacing_local_voice_or_screen(
    tmp_path,
) -> None:
    app = tmp_path / "app"
    log = tmp_path / "commands.log"
    (app / ".venv/bin").mkdir(parents=True)
    (app / "scripts").mkdir()
    (app / "secrets.env").write_text(
        "MAGPIE_CF_ACCESS_CLIENT_ID=machine-id.access\n"
        "MAGPIE_CF_ACCESS_CLIENT_SECRET=machine-secret\n",
        encoding="utf-8",
    )
    _executable(
        app / ".venv/bin/searchable-storage-edge",
        (
            "#!/bin/sh\n"
            "arguments=\"$*\"\n"
            "secrets_path=\"\"\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = --secrets-path ]; then shift; secrets_path=\"$1\"; fi\n"
            "  shift\n"
            "done\n"
            f"printf 'edge %s\\n' \"$arguments\" >> {log}\n"
            "printf '%s\\n' 'MAGPIE_EDGE_TOKEN=abcdefghijklmnopqrstuvwxyz_123456' >> \"$secrets_path\"\n"
        ),
    )
    for name in (
        "install-pi5-edge-service.sh",
        "install-pi5-voice-service.sh",
        "install-pi5-kiosk.sh",
    ):
        _executable(
            app / "scripts" / name,
            f"#!/bin/sh\nprintf '{name} central=%s voice=%s screen=%s device=%s workspace=%s\\n' \"${{MAGPIE_CENTRAL_URL:-}}\" \"${{MAGPIE_VOICE_API_URL:-}}\" \"${{MAGPIE_SCREEN_API_URL:-}}\" \"$MAGPIE_EDGE_DEVICE_ID\" \"$MAGPIE_EDGE_WORKSPACE_ID\" >> {log}\n",
        )
    fake_systemctl = tmp_path / "systemctl"
    fake_curl = tmp_path / "curl"
    _executable(fake_systemctl, "#!/bin/sh\nexit 0\n")
    _executable(fake_curl, "#!/bin/sh\nprintf '%s\\n' '{\"ok\":true}'\n")

    result = subprocess.run(
        [
            str(ROOT / "scripts/connect-pi5-cabinet.sh"),
            "--app-dir",
            str(app),
            "--central-url",
            "https://magpie.example",
            "--device-id",
            "main-cabinet",
            "--workspace-id",
            "workspace-1",
            "--pairing-code",
            "temporary-pairing-code",
        ],
        env={
            **os.environ,
            "PYTHON_BIN": sys.executable,
            "SYSTEMCTL_BIN": str(fake_systemctl),
            "CURL_BIN": str(fake_curl),
            "MAGPIE_SKIP_CENTRAL_PREFLIGHT": "1",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    commands = log.read_text(encoding="utf-8")
    assert "--pairing-code temporary-pairing-code" in commands
    assert "install-pi5-edge-service.sh central=https://magpie.example" in commands
    assert "install-pi5-voice-service.sh" not in commands
    assert "install-pi5-kiosk.sh" not in commands
    assert "device=main-cabinet workspace=workspace-1" in commands
    assert "Cabinet edge connected to https://magpie.example" in result.stdout
    assert "Local voice and kiosk remain unchanged" in result.stdout
    assert "--activate-central-experience" in result.stdout


def test_cabinet_connector_explicitly_activates_central_voice_and_screen(
    tmp_path,
) -> None:
    app = tmp_path / "app"
    log = tmp_path / "commands.log"
    (app / ".venv/bin").mkdir(parents=True)
    (app / "scripts").mkdir()
    (app / "secrets.env").write_text(
        "MAGPIE_EDGE_TOKEN=abcdefghijklmnopqrstuvwxyz_123456\n",
        encoding="utf-8",
    )
    _executable(app / ".venv/bin/searchable-storage-edge", "#!/bin/sh\nexit 0\n")
    for name in (
        "install-pi5-edge-service.sh",
        "install-pi5-voice-service.sh",
        "install-pi5-kiosk.sh",
    ):
        _executable(
            app / "scripts" / name,
            f"#!/bin/sh\nprintf '{name} central=%s voice=%s screen=%s device=%s workspace=%s\\n' \"${{MAGPIE_CENTRAL_URL:-}}\" \"${{MAGPIE_VOICE_API_URL:-}}\" \"${{MAGPIE_SCREEN_API_URL:-}}\" \"$MAGPIE_EDGE_DEVICE_ID\" \"$MAGPIE_EDGE_WORKSPACE_ID\" >> {log}\n",
        )
    fake_systemctl = tmp_path / "systemctl"
    fake_curl = tmp_path / "curl"
    _executable(fake_systemctl, "#!/bin/sh\nexit 0\n")
    _executable(fake_curl, "#!/bin/sh\nprintf '%s\\n' '{\"ok\":true}'\n")

    result = subprocess.run(
        [
            str(ROOT / "scripts/connect-pi5-cabinet.sh"),
            "--app-dir",
            str(app),
            "--central-url",
            "https://magpie.example",
            "--device-id",
            "main-cabinet",
            "--workspace-id",
            "workspace-1",
            "--activate-central-experience",
        ],
        env={
            **os.environ,
            "PYTHON_BIN": sys.executable,
            "SYSTEMCTL_BIN": str(fake_systemctl),
            "CURL_BIN": str(fake_curl),
            "MAGPIE_SKIP_CENTRAL_PREFLIGHT": "1",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    commands = log.read_text(encoding="utf-8")
    assert "install-pi5-edge-service.sh central=https://magpie.example" in commands
    assert "install-pi5-voice-service.sh central= voice=https://magpie.example" in commands
    assert "install-pi5-kiosk.sh central= voice= screen=https://magpie.example" in commands
    assert "Cabinet connected to https://magpie.example" in result.stdout


def test_cabinet_connector_rolls_back_partial_central_installer_failure(
    tmp_path,
) -> None:
    app, units, log, environment = _cabinet_activation_fixture(
        tmp_path,
        kiosk_exit=23,
    )

    result = subprocess.run(
        [
            str(ROOT / "scripts/connect-pi5-cabinet.sh"),
            "--app-dir",
            str(app),
            "--central-url",
            "https://magpie.example",
            "--device-id",
            "main-cabinet",
            "--workspace-id",
            "workspace-1",
            "--activate-central-experience",
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 23
    assert (units / "searchable-storage-voice.service").read_text(
        encoding="utf-8"
    ) == "local voice\n"
    assert (units / "searchable-storage-kiosk.service").read_text(
        encoding="utf-8"
    ) == "local kiosk\n"
    assert not (units / "searchable-storage-screen.service").exists()
    assert "restoring local voice and kiosk services" in result.stderr
    assert "Previous local voice and kiosk service state restored" in result.stderr
    commands = log.read_text(encoding="utf-8")
    assert "voice installed" in commands
    assert "kiosk installed" in commands
    assert "systemctl --user daemon-reload" in commands
    assert "systemctl --user restart searchable-storage-voice.service" in commands
    assert "systemctl --user restart searchable-storage-kiosk.service" in commands


def test_cabinet_connector_rolls_back_when_central_screen_route_is_not_ready(
    tmp_path,
) -> None:
    app, units, _log, environment = _cabinet_activation_fixture(
        tmp_path,
        bridge_ready=False,
    )

    result = subprocess.run(
        [
            str(ROOT / "scripts/connect-pi5-cabinet.sh"),
            "--app-dir",
            str(app),
            "--central-url",
            "https://magpie.example",
            "--device-id",
            "main-cabinet",
            "--workspace-id",
            "workspace-1",
            "--activate-central-experience",
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "central screen and voice readiness was not verified" in result.stderr
    assert "Previous local voice and kiosk service state restored" in result.stderr
    assert (units / "searchable-storage-voice.service").read_text(
        encoding="utf-8"
    ) == "local voice\n"
    assert (units / "searchable-storage-kiosk.service").read_text(
        encoding="utf-8"
    ) == "local kiosk\n"
    assert not (units / "searchable-storage-screen.service").exists()


def test_cabinet_connector_rejects_incomplete_cloudflare_machine_access(tmp_path) -> None:
    app = tmp_path / "app"
    (app / ".venv/bin").mkdir(parents=True)
    (app / "scripts").mkdir()
    (app / "secrets.env").write_text(
        "MAGPIE_CF_ACCESS_CLIENT_ID=machine-id.access\n", encoding="utf-8"
    )
    for path in (
        app / ".venv/bin/searchable-storage-edge",
        app / "scripts/install-pi5-edge-service.sh",
        app / "scripts/install-pi5-voice-service.sh",
        app / "scripts/install-pi5-kiosk.sh",
    ):
        _executable(path, "#!/bin/sh\nexit 0\n")

    result = subprocess.run(
        [
            str(ROOT / "scripts/connect-pi5-cabinet.sh"),
            "--app-dir",
            str(app),
            "--central-url",
            "https://magpie.example",
            "--workspace-id",
            "workspace-1",
            "--pairing-code",
            "temporary-pairing-code",
        ],
        env={**os.environ, "PYTHON_BIN": sys.executable},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "both MAGPIE_CF_ACCESS" in result.stderr




def _secure_origin_fake_curl(tmp_path: Path) -> Path:
    fake = tmp_path / "curl"
    _executable(
        fake,
        """#!/bin/sh
headers=''
body=''
url=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dump-header) headers="$2"; shift 2 ;;
    --output) body="$2"; shift 2 ;;
    --write-out|--max-time|--header) shift 2 ;;
    --silent|--show-error) shift ;;
    *) url="$1"; shift ;;
  esac
done
if [ "${FAKE_CURL_MODE:-access}" = access ]; then
  printf 'HTTP/2 302\\r\\nLocation: https://team.cloudflareaccess.com/cdn-cgi/access/login/magpie\\r\\n\\r\\n' > "$headers"
  : > "$body"
  printf 302
elif echo "$url" | grep -q manifest.webmanifest; then
  printf 'HTTP/2 200\\r\\n\\r\\n' > "$headers"
  printf '%s' '{"short_name":"Magpie","display":"standalone"}' > "$body"
  printf 200
elif echo "$url" | grep -q /auth/config; then
  printf 'HTTP/2 200\\r\\n\\r\\n' > "$headers"
  printf '{"schema":"magpie.auth.config.v1","mode":"supabase","multi_workspace":true,"supabase_url":"https://project.supabase.co","publishable_key":"%s"}' "${FAKE_PUBLISHABLE_KEY:-sb_publishable_1234567890abcdefghij_12345678}" > "$body"
  printf 200
elif echo "$url" | grep -q /health; then
  printf 'HTTP/2 200\r\n\r\n' > "$headers"
  if [ "${FAKE_CENTRAL_HEALTH:-central}" = pi ]; then
    printf '%s' '{"ok":true,"backend":"wled","pixel_count":182,"storage":{"backend":"supabase","workspace_id":"fixed"},"auth":{"mode":"none","multi_workspace":false,"workspace_id":"fixed"},"edge":{"mode":"local","routing":"local"}}' > "$body"
  else
    printf '%s' '{"ok":true,"backend":"none","pixel_count":0,"storage":{"backend":"supabase","workspace_id":"request-scoped"},"auth":{"mode":"supabase","multi_workspace":true,"workspace_id":"request-scoped"},"edge":{"mode":"workspace","routing":"location_light_target"}}' > "$body"
  fi
  printf 200
else
  printf 'HTTP/2 200\\r\\n\\r\\n' > "$headers"
  printf '{"commit":"%s","label":"v0.1.0 %s"}' "${FAKE_BUILD_COMMIT:-abc123}" "${FAKE_BUILD_COMMIT:-abc123}" > "$body"
  printf 200
fi
""",
    )
    return fake


def test_secure_origin_check_accepts_cloudflare_access_challenge(tmp_path) -> None:
    fake_curl = _secure_origin_fake_curl(tmp_path)

    result = subprocess.run(
        [str(ROOT / "scripts/check-secure-origin.sh")],
        env={
            **os.environ,
            "CURL_BIN": str(fake_curl),
            "MAGPIE_PUBLIC_ORIGIN": "https://magpie.example",
            "MAGPIE_ORIGIN_MODE": "access",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "edge origin is online and protected by Cloudflare Access" in result.stdout


def test_secure_origin_check_validates_authenticated_pwa(tmp_path) -> None:
    fake_curl = _secure_origin_fake_curl(tmp_path)

    result = subprocess.run(
        [str(ROOT / "scripts/check-secure-origin.sh")],
        env={
            **os.environ,
            "CURL_BIN": str(fake_curl),
            "FAKE_CURL_MODE": "authenticated",
            "MAGPIE_PUBLIC_ORIGIN": "https://magpie.example",
            "MAGPIE_ORIGIN_MODE": "access",
            "CF_ACCESS_CLIENT_ID": "client-id",
            "CF_ACCESS_CLIENT_SECRET": "client-secret",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "edge origin is authenticated and healthy" in result.stdout


def test_public_origin_check_validates_customer_auth_bootstrap(tmp_path) -> None:
    fake_curl = _secure_origin_fake_curl(tmp_path)

    result = subprocess.run(
        [str(ROOT / "scripts/check-secure-origin.sh")],
        env={
            **os.environ,
            "CURL_BIN": str(fake_curl),
            "FAKE_CURL_MODE": "authenticated",
            "MAGPIE_PUBLIC_ORIGIN": "https://magpie.example",
            "MAGPIE_ORIGIN_MODE": "public",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Public Magpie signup and PWA origin is healthy" in result.stdout


def test_public_origin_check_rejects_invalid_browser_key(tmp_path) -> None:
    fake_curl = _secure_origin_fake_curl(tmp_path)

    result = subprocess.run(
        [str(ROOT / "scripts/check-secure-origin.sh")],
        env={
            **os.environ,
            "CURL_BIN": str(fake_curl),
            "FAKE_CURL_MODE": "authenticated",
            "FAKE_PUBLISHABLE_KEY": "unrelated-notification",
            "MAGPIE_PUBLIC_ORIGIN": "https://magpie.example",
            "MAGPIE_ORIGIN_MODE": "public",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "invalid browser key" in result.stderr
    assert "unrelated-notification" not in result.stderr


def test_public_origin_check_rejects_cloudflare_access_challenge(tmp_path) -> None:
    fake_curl = _secure_origin_fake_curl(tmp_path)

    result = subprocess.run(
        [str(ROOT / "scripts/check-secure-origin.sh")],
        env={
            **os.environ,
            "CURL_BIN": str(fake_curl),
            "MAGPIE_PUBLIC_ORIGIN": "https://magpie.example",
            "MAGPIE_ORIGIN_MODE": "public",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "customer sign-in must not require Cloudflare Access" in result.stderr


def test_public_origin_check_requires_the_monitored_release_commit(tmp_path) -> None:
    fake_curl = _secure_origin_fake_curl(tmp_path)
    expected_commit = "a" * 40

    result = subprocess.run(
        [str(ROOT / "scripts/check-secure-origin.sh")],
        env={
            **os.environ,
            "CURL_BIN": str(fake_curl),
            "FAKE_CURL_MODE": "authenticated",
            "FAKE_BUILD_COMMIT": "b" * 40,
            "MAGPIE_EXPECTED_COMMIT": expected_commit,
            "MAGPIE_PUBLIC_ORIGIN": "https://magpie.example",
            "MAGPIE_ORIGIN_MODE": "public",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert f"expected {expected_commit}" in result.stderr
    assert f"received {'b' * 40}" in result.stderr


def test_public_origin_check_rejects_pi_hardware_origin(tmp_path) -> None:
    fake_curl = _secure_origin_fake_curl(tmp_path)

    result = subprocess.run(
        [str(ROOT / "scripts/check-secure-origin.sh")],
        env={
            **os.environ,
            "CURL_BIN": str(fake_curl),
            "FAKE_CURL_MODE": "authenticated",
            "FAKE_CENTRAL_HEALTH": "pi",
            "MAGPIE_PUBLIC_ORIGIN": "https://magpie.example",
            "MAGPIE_ORIGIN_MODE": "public",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "origin still exposes a hardware controller" in result.stderr






def test_cloudflare_installer_restarts_an_existing_connector() -> None:
    script = (ROOT / "scripts/install-pi5-cloudflare-tunnel.sh").read_text(
        encoding="utf-8"
    )

    assert 'systemctl --user enable "$SERVICE_NAME.service"' in script
    assert 'systemctl --user restart "$SERVICE_NAME.service"' in script
    assert 'systemctl --user enable --now "$SERVICE_NAME.service"' not in script


def test_central_cloudflare_installer_is_separate_and_health_gated() -> None:
    script = (ROOT / "scripts/install-central-cloudflare-tunnel.sh").read_text(
        encoding="utf-8"
    )

    assert "magpie-central-cloudflared" in script
    assert "http://127.0.0.1:8766" in script
    assert 'curl --fail --silent --show-error --max-time 10 "$health"' in script
    assert "MAGPIE_LOCAL_ORIGIN must be a loopback HTTP origin" in script
    assert "dedicated central-service tunnel token" in script
    assert 'systemctl --user enable "$SERVICE_NAME.service"' in script
    assert 'systemctl --user restart "$SERVICE_NAME.service"' in script
    assert "public hostname must not use Access" in script
    assert "edge hostname must use Service Auth" in script


def test_pi_voice_installer_provisions_optional_local_speech_fallback() -> None:
    script = (ROOT / "scripts/install-pi5-voice-service.sh").read_text(
        encoding="utf-8"
    )

    assert "command -v espeak-ng" in script
    assert "sudo -n true" in script
    assert "sudo apt-get install -y -qq espeak-ng" in script
    assert "Local speech fallback:" in script


def test_mobile_deployment_documents_central_tunnel_installation() -> None:
    guide = (ROOT / "docs/mobile-deployment.md").read_text(encoding="utf-8")

    assert "scripts/install-central-cloudflare-tunnel.sh" in guide
    assert "does not create or alter DNS" in guide


def test_supabase_switch_script_migrates_then_switches_atomically(tmp_path) -> None:
    app, log = _supabase_test_app(tmp_path)

    result = subprocess.run(
        [str(ROOT / "scripts/configure-pi-supabase.sh")],
        env=_supabase_env(app, tmp_path),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    config = json.loads((app / "config/led-controller.json").read_text(encoding="utf-8"))
    assert config["storage"]["backend"] == "supabase"
    assert config["storage"]["workspace_id"] == "magpie"
    assert config["zebra"]["base_url"] == "https://magpie.example"
    assert "SUPABASE_SERVICE_ROLE_KEY=service-role-secret" in (
        app / "secrets.env"
    ).read_text(encoding="utf-8")
    assert (app / "secrets.env").stat().st_mode & 0o777 == 0o600
    assert list((app / "state").glob("inventory.pre-supabase-*.db"))
    commands = log.read_text(encoding="utf-8")
    assert "systemctl --user stop searchable-storage-led.service" in commands
    assert commands.index("migrate ") < commands.index("check ")
    assert "--workspace-id magpie" in commands
    assert "systemctl --user restart searchable-storage-led.service" in commands


def test_supabase_switch_script_restarts_sqlite_after_migration_failure(tmp_path) -> None:
    app, log = _supabase_test_app(tmp_path, migration_exit=1)
    original = (app / "config/led-controller.json").read_text(encoding="utf-8")

    result = subprocess.run(
        [str(ROOT / "scripts/configure-pi-supabase.sh")],
        env=_supabase_env(app, tmp_path),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert (app / "config/led-controller.json").read_text(encoding="utf-8") == original
    assert "systemctl --user restart searchable-storage-led.service" in log.read_text(
        encoding="utf-8"
    )




def test_supabase_schema_workflow_proves_clean_postgres_install() -> None:
    workflow = Path(".github/workflows/check-supabase-schema.yml").read_text(
        encoding="utf-8"
    )

    assert "image: postgres:17-alpine" in workflow
    assert "find supabase/migrations -maxdepth 1 -type f -name '*.sql' | sort" in workflow
    assert "psql --set ON_ERROR_STOP=1 --file \"$migration\"" in workflow
    assert "public.magpie_schema_capabilities()" in workflow
    assert "location_content_revisions" in workflow
    assert "to_regclass('public.active_guidance_states')" in workflow
