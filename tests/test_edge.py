import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from searchable_storage_led.api import create_app
from searchable_storage_led.config import EdgeConfig, LedConfig, ZebraConfig
from searchable_storage_led.edge import (
    QueuedEdgeWriter,
    SqliteEdgeRepository,
    SupabaseEdgeRepository,
    WorkspaceEdgeRepositoryRouter,
    WorkspaceQueuedEdgeWriter,
    normalize_credential_scopes,
)
from searchable_storage_led.edge_worker import (
    EdgeWorkerError,
    discover_local_capabilities,
    execute_local_command,
    load_edge_machine_auth,
    pair_device,
    run_once,
    store_edge_token,
    validate_worker_urls,
)


def test_sqlite_edge_queue_claim_complete_and_status(tmp_path) -> None:
    repository = SqliteEdgeRepository(tmp_path / "edge.db")
    queued = repository.enqueue(
        "cabinet-1", "set_pixels", {"pixels": [[1, 2, 3]]}, ttl_seconds=60
    )

    claimed = repository.claim("cabinet-1", claim_timeout=5, max_attempts=3)
    assert claimed is not None
    assert claimed.id == queued.id
    assert claimed.status == "claimed"
    assert claimed.attempts == 1

    completed = repository.complete("cabinet-1", queued.id, succeeded=True)
    assert completed is not None
    assert completed.status == "succeeded"
    assert repository.claim("cabinet-1", claim_timeout=5, max_attempts=3) is None
    assert repository.status()["counts"] == [
        {"device_id": "cabinet-1", "status": "succeeded", "count": 1}
    ]


def test_sqlite_edge_credentials_are_hashed_rotatable_and_revocable(tmp_path) -> None:
    repository = SqliteEdgeRepository(tmp_path / "edge.db")
    first_hash = hashlib.sha256(b"first-secret").hexdigest()
    second_hash = hashlib.sha256(b"second-secret").hexdigest()

    enrolled = repository.set_credential(
        "cabinet-1",
        token_hash=first_hash,
        token_hint="secret",
        scopes=["voice", "screen"],
    )

    assert enrolled["enrolled"] is True
    assert enrolled["credential_scopes"] == ["screen", "voice"]
    assert repository.credential_matches("cabinet-1", first_hash) is True
    assert repository.credential_authorizes("cabinet-1", first_hash, "voice") is True
    assert repository.credential_authorizes("cabinet-1", first_hash, "commands") is False
    assert repository.credential_matches("cabinet-1", second_hash) is False
    status = repository.status()["devices"][0]
    assert status["credential_hint"] == "secret"
    assert status["credential_scopes"] == ["screen", "voice"]
    assert status["enrolled"] is True
    assert "credential_hash" not in status

    repository.set_credential(
        "cabinet-1",
        token_hash=second_hash,
        token_hint="ecret2",
        scopes=["commands"],
    )
    assert repository.credential_matches("cabinet-1", first_hash) is False
    assert repository.credential_matches("cabinet-1", second_hash) is True
    assert repository.credential_authorizes("cabinet-1", second_hash, "commands") is True
    assert repository.credential_authorizes("cabinet-1", second_hash, "voice") is False

    revoked = repository.revoke_credential("cabinet-1")
    assert revoked is not None
    assert revoked["enrolled"] is False
    assert repository.credential_matches("cabinet-1", second_hash) is False
    assert repository.credential_authorizes("cabinet-1", second_hash, "commands") is False


def test_sqlite_edge_pairing_is_expiring_one_use_and_keeps_secret_digests(
    tmp_path,
) -> None:
    repository = SqliteEdgeRepository(tmp_path / "edge.db")
    pairing_hash = hashlib.sha256(b"temporary-pairing-secret").hexdigest()
    token_hash = hashlib.sha256(b"permanent-device-secret").hexdigest()

    pending = repository.set_pairing(
        "cabinet-1",
        pairing_hash=pairing_hash,
        pairing_hint="secret",
        scopes=["commands", "voice"],
        expires_at=time.time() + 60,
    )
    assert pending["pairing_pending"] is True
    status = repository.status()["devices"][0]
    assert status["pairing_pending"] is True
    assert status["pairing_scopes"] == ["commands", "voice"]
    assert pairing_hash not in repr(status)

    assert repository.consume_pairing(
        "cabinet-1",
        pairing_hash=hashlib.sha256(b"wrong").hexdigest(),
        token_hash=token_hash,
        token_hint="secret",
    ) is None
    paired = repository.consume_pairing(
        "cabinet-1",
        pairing_hash=pairing_hash,
        token_hash=token_hash,
        token_hint="secret",
    )
    assert paired is not None
    assert paired["credential_scopes"] == ["commands", "voice"]
    assert repository.credential_matches("cabinet-1", token_hash) is True
    assert repository.consume_pairing(
        "cabinet-1",
        pairing_hash=pairing_hash,
        token_hash=hashlib.sha256(b"replacement").hexdigest(),
        token_hint="ement",
    ) is None

    repository.set_pairing(
        "expired",
        pairing_hash=pairing_hash,
        pairing_hint="secret",
        scopes=["screen"],
        expires_at=time.time() - 1,
    )
    assert repository.consume_pairing(
        "expired",
        pairing_hash=pairing_hash,
        token_hash=token_hash,
        token_hint="secret",
    ) is None


def test_edge_credential_scope_normalization_rejects_empty_and_unknown() -> None:
    assert normalize_credential_scopes(None) == ["commands"]
    assert normalize_credential_scopes([" Voice ", "commands", "voice"]) == [
        "commands",
        "voice",
    ]
    with pytest.raises(ValueError, match="at least one"):
        normalize_credential_scopes([])
    with pytest.raises(ValueError, match="unsupported"):
        normalize_credential_scopes(["admin"])


def test_supabase_edge_credentials_use_workspace_scoped_digest_operations() -> None:
    class CredentialClient:
        def __init__(self) -> None:
            self.row = {}
            self.calls = []

        def upsert(self, table, values, *, on_conflict):
            self.calls.append(("upsert", table, values, on_conflict))
            self.row = {**self.row, **values}
            return [self.row]

        def select(self, table, *, columns="*", filters=None, order="", limit=None):
            self.calls.append(("select", table, columns, filters, limit))
            if filters and filters.get("device_id") != "eq.cabinet-1":
                return []
            return [self.row] if self.row else []

        def update(self, table, values, *, filters, return_rows=True):
            self.calls.append(("update", table, values, filters))
            if not self.row:
                return []
            self.row = {**self.row, **values}
            return [self.row]

    client = CredentialClient()
    repository = SupabaseEdgeRepository(client, workspace_id="workspace-1")
    token_hash = hashlib.sha256(b"hosted-secret").hexdigest()

    repository.set_credential(
        "cabinet-1",
        token_hash=token_hash,
        token_hint="secret",
        scopes=["commands", "voice"],
    )

    assert client.row["workspace_id"] == "workspace-1"
    assert client.row["credential_hash"] == token_hash
    assert client.row["credential_scopes"] == ["commands", "voice"]
    assert repository.credential_matches("cabinet-1", token_hash) is True
    assert repository.credential_authorizes("cabinet-1", token_hash, "voice") is True
    assert repository.credential_authorizes("cabinet-1", token_hash, "screen") is False
    assert repository.credential_matches("other-device", token_hash) is False
    assert repository.revoke_credential("cabinet-1") is not None
    assert repository.credential_matches("cabinet-1", token_hash) is False
    assert repository.credential_authorizes("cabinet-1", token_hash, "voice") is False


def test_supabase_edge_pairing_uses_atomic_workspace_scoped_rpc() -> None:
    class PairingClient:
        def __init__(self) -> None:
            self.row = {}
            self.rpc_call = None

        def upsert(self, table, values, *, on_conflict):
            self.row = dict(values)
            return [self.row]

        def rpc(self, name, payload):
            self.rpc_call = (name, payload)
            return {
                "device_id": payload["target_device_id"],
                "credential_hint": payload["new_credential_hint"],
                "credential_scopes": ["commands", "screen"],
            }

    client = PairingClient()
    repository = SupabaseEdgeRepository(client, workspace_id="workspace-1")
    pending = repository.set_pairing(
        "cabinet-1",
        pairing_hash="pair-digest",
        pairing_hint="digest",
        scopes=["screen", "commands"],
        expires_at=time.time() + 60,
    )
    paired = repository.consume_pairing(
        "cabinet-1",
        pairing_hash="pair-digest",
        token_hash="token-digest",
        token_hint="digest",
    )

    assert pending["pairing_scopes"] == ["commands", "screen"]
    assert client.row["workspace_id"] == "workspace-1"
    assert client.row["pairing_hash"] == "pair-digest"
    assert client.rpc_call == (
        "magpie_consume_edge_pairing",
        {
            "target_workspace_id": "workspace-1",
            "target_device_id": "cabinet-1",
            "presented_pairing_hash": "pair-digest",
            "new_credential_hash": "token-digest",
            "new_credential_hint": "digest",
        },
    )
    assert paired["credential_scopes"] == ["commands", "screen"]


def test_sqlite_edge_queue_reclaims_abandoned_command(tmp_path) -> None:
    repository = SqliteEdgeRepository(tmp_path / "edge.db")
    queued = repository.enqueue("cabinet-1", "set_pixels", {}, ttl_seconds=60)
    assert repository.claim("cabinet-1", claim_timeout=5, max_attempts=3) is not None

    with repository._connect() as conn:
        conn.execute(
            "UPDATE edge_commands SET claimed_at=? WHERE id=?",
            (time.time() - 10, queued.id),
        )

    reclaimed = repository.claim("cabinet-1", claim_timeout=5, max_attempts=3)
    assert reclaimed is not None
    assert reclaimed.id == queued.id
    assert reclaimed.attempts == 2


def test_new_pixel_state_supersedes_unclaimed_state(tmp_path) -> None:
    repository = SqliteEdgeRepository(tmp_path / "edge.db")
    old = repository.enqueue(
        "cabinet-1", "set_pixels", {"pixels": [[1, 0, 0]]}, ttl_seconds=60
    )
    new = repository.enqueue(
        "cabinet-1", "set_pixels", {"pixels": [[0, 1, 0]]}, ttl_seconds=60
    )

    claimed = repository.claim("cabinet-1", claim_timeout=5, max_attempts=3)

    assert claimed is not None
    assert claimed.id == new.id
    recent = repository.status()["recent_commands"]
    assert (
        next(command for command in recent if command["id"] == old.id)["error"]
        == "superseded by newer state"
    )


def test_print_commands_remain_fifo_instead_of_superseding(tmp_path) -> None:
    repository = SqliteEdgeRepository(tmp_path / "edge.db")
    first = repository.enqueue(
        "printer-1", "print_zpl", {"zpl": "^XA^FDOne^FS^XZ"}, ttl_seconds=60
    )
    second = repository.enqueue(
        "printer-1", "print_zpl", {"zpl": "^XA^FDTwo^FS^XZ"}, ttl_seconds=60
    )

    claimed_first = repository.claim("printer-1", claim_timeout=5, max_attempts=3)
    assert claimed_first is not None
    assert claimed_first.id == first.id
    repository.complete("printer-1", first.id, succeeded=True)
    claimed_second = repository.claim("printer-1", claim_timeout=5, max_attempts=3)
    assert claimed_second is not None
    assert claimed_second.id == second.id


def test_retryable_failure_requeues_with_backoff(tmp_path) -> None:
    repository = SqliteEdgeRepository(tmp_path / "edge.db")
    queued = repository.enqueue("cabinet-1", "set_pixels", {}, ttl_seconds=60)
    assert repository.claim("cabinet-1", claim_timeout=5, max_attempts=3) is not None

    retried = repository.complete(
        "cabinet-1",
        queued.id,
        succeeded=False,
        error="local controller unavailable",
        retryable=True,
        max_attempts=3,
    )

    assert retried is not None
    assert retried.status == "queued"
    assert repository.claim("cabinet-1", claim_timeout=5, max_attempts=3) is None


def test_queued_writer_serializes_complete_pixel_state(tmp_path) -> None:
    repository = SqliteEdgeRepository(tmp_path / "edge.db")
    enqueued = []
    writer = QueuedEdgeWriter(
        repository,
        device_id="cabinet-1",
        ttl_seconds=60,
        on_enqueue=enqueued.append,
    )

    writer.write_pixels(
        [(0, 0, 0), (5, 10, 15)], LedConfig(pixel_count=2, backend="none")
    )

    command = repository.claim("cabinet-1", claim_timeout=5, max_attempts=3)
    assert command is not None
    assert [event.id for event in enqueued] == [command.id]
    assert command.payload == {"pixels": [[0, 0, 0], [5, 10, 15]], "pixel_count": 2}


def test_workspace_writer_routes_explicit_workspace_without_request_context(tmp_path) -> None:
    router = WorkspaceEdgeRepositoryRouter(
        lambda workspace_id: SqliteEdgeRepository(
            tmp_path / f"{workspace_id}.db", workspace_id=workspace_id
        ),
        lambda: "request-scoped",
    )
    writer = WorkspaceQueuedEdgeWriter(
        router,
        workspace_id=lambda: "request-scoped",
        ttl_seconds=60,
    )

    writer.write_pixels(
        "cabinet-1",
        [(5, 10, 15)],
        pixel_count=1,
        workspace_id="workspace-a",
    )

    command = router.for_workspace("workspace-a").claim(
        "cabinet-1", claim_timeout=5, max_attempts=3
    )
    assert command is not None
    assert command.payload == {"pixels": [[5, 10, 15]], "pixel_count": 1}
    assert router.for_workspace("workspace-b").claim(
        "cabinet-1", claim_timeout=5, max_attempts=3
    ) is None


def test_edge_api_authenticates_device_and_runs_queue(tmp_path, monkeypatch) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []})
    )
    monkeypatch.setenv("TEST_EDGE_TOKENS", json.dumps({"cabinet-1": "secret-token"}))
    cfg = LedConfig(
        pixel_count=2,
        backend="none",
        layout_path=str(layout),
        database_path=str(tmp_path / "app.db"),
        edge=EdgeConfig(
            mode="central", target_device_id="cabinet-1", tokens_env="TEST_EDGE_TOKENS"
        ),
    )
    repository = SqliteEdgeRepository(tmp_path / "edge.db")
    repository.set_credential(
        "cabinet-1",
        token_hash=hashlib.sha256(b"secret-token").hexdigest(),
        token_hint="-token",
        scopes=["commands", "screen", "voice"],
    )
    client = TestClient(create_app(cfg, edge_repository=repository))

    assert client.get("/capabilities").json()["features"]["edge_long_poll"] is True
    assert client.get("/capabilities").json()["features"]["edge_voice_bridge"] is True
    assert client.post("/edge/devices/cabinet-1/commands/claim").status_code == 401
    headers = {"Authorization": "Bearer secret-token"}
    heartbeat = client.post(
        "/edge/devices/cabinet-1/heartbeat",
        headers=headers,
        json={"version": "v1", "capabilities": ["set_pixels"]},
    )
    assert heartbeat.status_code == 200
    device_status = client.get(
        "/edge/devices/cabinet-1/status",
        headers=headers,
    )
    assert device_status.status_code == 200
    status_body = device_status.json()
    assert status_body["schema"] == "magpie.edge.device_status.v1"
    assert status_body["device"]["device_id"] == "cabinet-1"
    assert status_body["device"]["version"] == "v1"
    assert status_body["device"]["command_ready"] is True
    assert status_body["device"]["service_readiness"] == {
        "lights": {
            "scope": "commands",
            "capability": "set_pixels",
            "granted": True,
            "reported": True,
            "ready": True,
        },
        "voice": {
            "scope": "voice",
            "capability": "voice_assistant",
            "granted": True,
            "reported": False,
            "ready": False,
        },
        "screen": {
            "scope": "screen",
            "capability": "kiosk_display",
            "granted": True,
            "reported": False,
            "ready": False,
        },
        "printer": {
            "scope": "printer",
            "capability": "label_printer",
            "granted": False,
            "reported": False,
            "ready": False,
        },
        "check_station": {
            "scope": "check_station",
            "capability": "check_station",
            "granted": False,
            "reported": False,
            "ready": False,
        },
    }
    assert "credential_hash" not in repr(status_body)
    assert "secret-token" not in repr(status_body)
    assert client.get(
        "/edge/devices/cabinet-1/status",
        headers={"Authorization": "Bearer wrong-token"},
    ).status_code == 401
    assert client.get(
        "/edge/devices/other-cabinet/status",
        headers=headers,
    ).status_code == 401

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as executor:
        waiting = executor.submit(
            client.post,
            "/edge/devices/cabinet-1/commands/claim?wait_seconds=2",
            headers=headers,
        )
        time.sleep(0.05)
        assert (
            client.post(
                "/pixels",
                json={
                    "pixels": [[1, [255, 0, 0]]],
                    "clear_others": True,
                    "brightness": 1.0,
                },
            ).status_code
            == 200
        )
        claim_response = waiting.result(timeout=2)
    claim_body = claim_response.json()
    claim = claim_body["command"]
    assert claim_body["timed_out"] is False
    assert time.monotonic() - started < 1
    assert claim["kind"] == "set_pixels"
    assert claim["payload"]["pixels"] == [[0, 0, 0], [255, 0, 0]]

    completed = client.post(
        f"/edge/devices/cabinet-1/commands/{claim['id']}/complete",
        headers=headers,
        json={"succeeded": True},
    )
    assert completed.status_code == 200
    assert (
        client.get("/edge/status").json()["recent_commands"][0]["status"] == "succeeded"
    )
    timeout_started = time.monotonic()
    idle = client.post(
        "/edge/devices/cabinet-1/commands/claim?wait_seconds=0.05",
        headers=headers,
    ).json()
    assert idle == {"ok": True, "command": None, "timed_out": True}
    assert time.monotonic() - timeout_started >= 0.04


def test_label_print_queues_to_connected_edge_printer(tmp_path) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "software-only", "columns": 1, "rows_bottom_to_top": []})
    )
    repository = SqliteEdgeRepository(tmp_path / "edge.db")
    repository.set_credential(
        "printer-1",
        token_hash=hashlib.sha256(b"printer-token").hexdigest(),
        token_hint="-token",
        scopes=["commands", "printer"],
    )
    repository.heartbeat(
        "printer-1",
        version="printer-v1",
        capabilities=["label_printer"],
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout),
                database_path=str(tmp_path / "app.db"),
                edge=EdgeConfig(
                    mode="central",
                    target_device_id="printer-1",
                    command_ttl_seconds=90,
                ),
            ),
            edge_repository=repository,
        )
    )
    created = client.post(
        "/locations",
        json={"type": "box", "label": "Roaster Parts"},
    )
    assert created.status_code == 200

    status = client.get("/labels/printer/status").json()
    printed = client.post("/locations/roaster-parts/print-label")

    assert status["ok"] is True
    assert status["edge"] is True
    assert status["configured"]["device_ids"] == ["printer-1"]
    assert printed.status_code == 200
    receipt = printed.json()
    assert receipt["queued_label"] == "roaster-parts"
    assert receipt["printer"] == "edge:printer-1"
    assert receipt["status"] == "queued"
    command = repository.claim("printer-1", claim_timeout=5, max_attempts=3)
    assert command is not None
    assert command.kind == "print_zpl"
    assert command.payload["location_id"] == "roaster-parts"
    assert command.payload["zpl"].startswith("^XA")
    assert command.payload["zpl"].rstrip().endswith("^XZ")
    repository.complete("printer-1", command.id, succeeded=True)

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "print the roaster parts label",
            "plan": [
                {
                    "tool": "print_label",
                    "arguments": {"location_id": "roaster-parts"},
                }
            ],
        },
    )
    assert confirmed.status_code == 200
    [applied] = confirmed.json()["applied"]
    assert applied["queued_label"] == "roaster-parts"
    assert applied["status"] == "queued"
    agent_command = repository.claim("printer-1", claim_timeout=5, max_attempts=3)
    assert agent_command is not None
    assert agent_command.id == applied["command_id"]


def test_local_print_endpoint_is_loopback_only(tmp_path, monkeypatch) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "printer", "columns": 1, "rows_bottom_to_top": []})
    )
    cfg = LedConfig(
        pixel_count=0,
        backend="none",
        layout_path=str(layout),
        database_path=str(tmp_path / "app.db"),
        zebra=ZebraConfig(host="192.0.2.10"),
    )
    sent = []
    monkeypatch.setattr(
        "searchable_storage_led.api.send_zpl",
        lambda zpl, zebra: sent.append((zpl, zebra.host)),
    )
    loopback = TestClient(create_app(cfg), client=("127.0.0.1", 50000))
    remote = TestClient(create_app(cfg), client=("192.0.2.20", 50000))

    response = loopback.post(
        "/edge/local/print-zpl",
        json={"zpl": "^XA^FDMagpie^FS^XZ"},
    )
    forbidden = remote.post(
        "/edge/local/print-zpl",
        json={"zpl": "^XA^FDMagpie^FS^XZ"},
    )

    assert response.status_code == 200
    assert response.json()["printer"] == "192.0.2.10"
    assert sent == [("^XA^FDMagpie^FS^XZ\n", "192.0.2.10")]
    assert forbidden.status_code == 403


def test_product_readiness_requires_managed_recent_edge_controller(tmp_path) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []})
    )
    cfg = LedConfig(
        pixel_count=2,
        backend="none",
        layout_path=str(layout),
        database_path=str(tmp_path / "app.db"),
        edge=EdgeConfig(
            mode="central",
            target_device_id="cabinet-1",
            tokens_env="UNSET_EDGE_TOKENS",
        ),
    )
    token = "managed-secret-token"
    repository = SqliteEdgeRepository(tmp_path / "edge.db")
    repository.set_credential(
        "cabinet-1",
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        token_hint=token[-6:],
        scopes=["commands"],
    )
    client = TestClient(create_app(cfg, edge_repository=repository))

    before_readiness = client.get("/product/readiness").json()
    before = {
        check["key"]: check
        for check in before_readiness["checks"]
    }
    assert before["secure_edge_controller"]["passed"] is False
    assert before["secure_edge_controller"]["required"] is True
    assert before_readiness["ready"] is False
    required_total = before_readiness["required_total"]
    hardware_group = next(
        group for group in before_readiness["groups"] if group["key"] == "hardware"
    )
    assert hardware_group["required"] is True

    heartbeat = client.post(
        "/edge/devices/cabinet-1/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"version": "v1", "capabilities": ["set_pixels"]},
    )
    assert heartbeat.status_code == 200

    after_readiness = client.get("/product/readiness").json()
    after = {
        check["key"]: check
        for check in after_readiness["checks"]
    }
    assert after["secure_edge_controller"]["passed"] is True
    assert after["secure_edge_controller"]["required"] is True
    assert after_readiness["required_total"] == required_total
    assert after_readiness["passed"] == before_readiness["passed"] + 1
    assert "enrolled=1/1" in after["secure_edge_controller"]["evidence"]
    assert "set_pixels" in after["secure_edge_controller"]["evidence"]


def test_product_readiness_requires_each_configured_edge_service(tmp_path) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "services", "columns": 1, "rows_bottom_to_top": []})
    )
    token = "service-device-secret-token"
    repository = SqliteEdgeRepository(tmp_path / "edge.db")
    repository.set_credential(
        "service-station",
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        token_hint=token[-6:],
        scopes=["voice", "screen", "printer", "check_station"],
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout),
                database_path=str(tmp_path / "app.db"),
            ),
            edge_repository=repository,
        )
    )

    initial = client.get("/product/readiness").json()
    initial_checks = {check["key"]: check for check in initial["checks"]}
    service_keys = {
        "secure_edge_voice",
        "secure_edge_screen",
        "secure_edge_printer",
        "secure_edge_check_station",
    }
    assert all(initial_checks[key]["required"] for key in service_keys)
    assert sum(initial_checks[key]["required"] for key in service_keys) == 4
    assert all(not initial_checks[key]["passed"] for key in service_keys)
    assert initial_checks["secure_edge_controller"]["required"] is False

    device_headers = {
        "Authorization": f"Bearer {token}",
        "X-Magpie-Device-ID": "service-station",
    }
    heartbeat = client.post(
        "/edge/devices/service-station/heartbeat",
        headers=device_headers,
        json={
            "version": "station-v1",
            "capabilities": ["label_printer", "check_station"],
        },
    )
    assert heartbeat.status_code == 200

    partial = client.get("/product/readiness").json()
    partial_checks = {check["key"]: check for check in partial["checks"]}
    assert partial_checks["secure_edge_printer"]["passed"] is True
    assert partial_checks["secure_edge_check_station"]["passed"] is True
    assert partial_checks["secure_edge_voice"]["passed"] is False
    assert partial_checks["secure_edge_screen"]["passed"] is False

    complete_heartbeat = client.post(
        "/edge/devices/service-station/heartbeat",
        headers=device_headers,
        json={
            "version": "station-v1",
            "capabilities": [
                "voice_assistant",
                "kiosk_display",
                "label_printer",
                "check_station",
            ],
        },
    )
    assert complete_heartbeat.status_code == 200

    ready = client.get("/product/readiness").json()
    ready_checks = {check["key"]: check for check in ready["checks"]}
    assert all(ready_checks[key]["passed"] for key in service_keys)
    assert ready["passed"] == initial["passed"] + 4


def test_local_executor_only_sends_lit_pixels() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        execute_local_command(
            {"kind": "set_pixels", "payload": {"pixels": [[0, 0, 0], [1, 2, 3]]}},
            local_url="http://127.0.0.1",
            client=client,
        )

    assert requests[0].url.path == "/pixels"
    assert json.loads(requests[0].content) == {
        "pixels": [[1, [1, 2, 3]]],
        "clear_others": True,
        "brightness": 1.0,
    }


def test_local_executor_forwards_validated_zpl_to_loopback_service() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        execute_local_command(
            {
                "kind": "print_zpl",
                "payload": {"zpl": "^XA^FDMagpie^FS^XZ", "location_id": "B001"},
            },
            local_url="http://127.0.0.1",
            client=client,
        )

    assert requests[0].url.path == "/edge/local/print-zpl"
    assert json.loads(requests[0].content) == {"zpl": "^XA^FDMagpie^FS^XZ"}


def test_local_executor_rejects_invalid_print_payload() -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _request: None)) as client:
        with pytest.raises(EdgeWorkerError, match="invalid ZPL"):
            execute_local_command(
                {"kind": "print_zpl", "payload": {"zpl": "not a label"}},
                local_url="http://127.0.0.1",
                client=client,
            )


def test_worker_heartbeats_executes_and_acknowledges() -> None:
    seen = []
    heartbeat_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.url.host,
                request.url.path,
                request.headers.get("authorization"),
                request.headers.get("x-magpie-workspace"),
            )
        )
        if request.url.path == "/edge/local-capabilities":
            return httpx.Response(
                200,
                json={
                    "capabilities": [
                        "set_pixels",
                        "voice_assistant",
                        "kiosk_display",
                    ]
                },
            )
        if request.url.path.endswith("/heartbeat"):
            heartbeat_payloads.append(json.loads(request.content))
        if request.url.path.endswith("/commands/claim"):
            return httpx.Response(
                200,
                json={
                    "command": {
                        "id": "command-1",
                        "kind": "set_pixels",
                        "payload": {"pixels": [[8, 0, 0]]},
                    }
                },
            )
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert run_once(
            central_url="https://central",
            local_url="http://127.0.0.1",
            device_id="cabinet-1",
            token="secret",
            workspace_id="workspace-1",
            client=client,
        )

    assert seen == [
        ("127.0.0.1", "/edge/local-capabilities", None, None),
        ("central", "/edge/devices/cabinet-1/heartbeat", "Bearer secret", "workspace-1"),
        ("central", "/edge/devices/cabinet-1/commands/claim", "Bearer secret", "workspace-1"),
        ("127.0.0.1", "/pixels", None, None),
        (
            "central",
            "/edge/devices/cabinet-1/commands/command-1/complete",
            "Bearer secret",
            "workspace-1",
        ),
    ]
    assert heartbeat_payloads == [
        {
            "version": "",
            "capabilities": [
                "kiosk_display",
                "set_pixels",
                "voice_assistant",
            ],
        }
    ]


def test_worker_executes_and_acknowledges_print_command() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.path, request.content))
        if request.url.path == "/edge/local-capabilities":
            return httpx.Response(200, json={"capabilities": ["label_printer"]})
        if request.url.path.endswith("/commands/claim"):
            return httpx.Response(
                200,
                json={
                    "command": {
                        "id": "print-1",
                        "kind": "print_zpl",
                        "payload": {"zpl": "^XA^FDMagpie^FS^XZ"},
                    }
                },
            )
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert run_once(
            central_url="https://central",
            local_url="http://127.0.0.1",
            device_id="printer-1",
            token="secret",
            workspace_id="workspace-1",
            client=client,
        )

    paths = [(host, path) for host, path, _content in seen]
    assert paths == [
        ("127.0.0.1", "/edge/local-capabilities"),
        ("central", "/edge/devices/printer-1/heartbeat"),
        ("central", "/edge/devices/printer-1/commands/claim"),
        ("127.0.0.1", "/edge/local/print-zpl"),
        ("central", "/edge/devices/printer-1/commands/print-1/complete"),
    ]
    completion = json.loads(seen[-1][2])
    assert completion == {"succeeded": True, "error": "", "retryable": False}


def test_worker_local_capability_discovery_filters_and_falls_back() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "current":
            return httpx.Response(
                200,
                json={
                    "capabilities": [
                        "voice_assistant",
                        "set_pixels",
                        "not_allowed",
                    ]
                },
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert discover_local_capabilities(
            local_url="http://current",
            client=client,
        ) == ["set_pixels", "voice_assistant"]
        assert discover_local_capabilities(
            local_url="http://legacy",
            client=client,
        ) == ["set_pixels"]


def test_worker_pairs_once_and_stores_only_the_permanent_token_locally(tmp_path) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.url.path,
                request.headers.get("x-magpie-workspace"),
                request.headers.get("cf-access-client-id"),
                request.headers.get("cf-access-client-secret"),
                json.loads(request.content),
            )
        )
        return httpx.Response(
            200,
            json={"ok": True, "token": "permanent-device-secret-1234567890"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        token = pair_device(
            central_url="https://central",
            device_id="cabinet-1",
            pairing_code="temporary-code",
            workspace_id="workspace-1",
            cf_access_client_id="machine-id.access",
            cf_access_client_secret="machine-secret",
            client=client,
        )

    secrets_path = tmp_path / "secrets.env"
    secrets_path.write_text("OPENAI_API_KEY=existing\nMAGPIE_EDGE_TOKEN=old\n")
    stored = store_edge_token(secrets_path, token)

    assert seen == [
        (
            "/edge/devices/cabinet-1/pair",
            "workspace-1",
            "machine-id.access",
            "machine-secret",
            {"pairing_code": "temporary-code"},
        )
    ]
    assert stored.read_text() == (
        "OPENAI_API_KEY=existing\n"
        "MAGPIE_EDGE_TOKEN=permanent-device-secret-1234567890\n"
    )
    assert stored.stat().st_mode & 0o777 == 0o600


def test_worker_rejects_malformed_pairing_credentials(tmp_path) -> None:
    with pytest.raises(EdgeWorkerError, match="invalid device credential"):
        store_edge_token(tmp_path / "secrets.env", "injected\nOTHER_SECRET=bad")


def test_worker_requires_complete_cloudflare_machine_credentials() -> None:
    with pytest.raises(ValueError, match="both MAGPIE_CF_ACCESS"):
        pair_device(
            central_url="https://central",
            device_id="cabinet-1",
            pairing_code="temporary-code",
            cf_access_client_id="machine-id.access",
        )


def test_worker_loads_only_machine_auth_from_protected_secrets(tmp_path) -> None:
    secrets_path = tmp_path / "secrets.env"
    secrets_path.write_text(
        "OPENAI_API_KEY=do-not-load\n"
        "MAGPIE_CF_ACCESS_CLIENT_ID=machine-id.access\n"
        "MAGPIE_CF_ACCESS_CLIENT_SECRET=machine-secret\n"
        "MAGPIE_EDGE_TOKEN=also-do-not-load\n"
    )

    assert load_edge_machine_auth(secrets_path) == (
        "machine-id.access",
        "machine-secret",
    )
    assert load_edge_machine_auth(tmp_path / "missing.env") == ("", "")


def test_idle_worker_long_polls_without_repeating_heartbeat() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.url.path,
                request.url.params.get("wait_seconds"),
                request.headers.get("authorization"),
            )
        )
        return httpx.Response(
            200,
            json={"ok": True, "command": None, "timed_out": True},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert run_once(
            central_url="https://central",
            local_url="http://127.0.0.1",
            device_id="cabinet-1",
            token="secret",
            heartbeat=False,
            wait_seconds=20,
            client=client,
        ) is False

    assert seen == [
        ("/edge/devices/cabinet-1/commands/claim", "20.0", "Bearer secret")
    ]


def test_worker_requires_https_central_and_loopback_controller() -> None:
    validate_worker_urls("https://magpie.example.com", "http://127.0.0.1:8765")

    with pytest.raises(ValueError, match="HTTPS"):
        validate_worker_urls("http://magpie.example.com", "http://127.0.0.1:8765")
    with pytest.raises(ValueError, match="loopback"):
        validate_worker_urls("https://magpie.example.com", "http://cabinet.lan:8765")
