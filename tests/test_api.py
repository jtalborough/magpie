import base64
import csv
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from searchable_storage_led.agent import AgentResult
from searchable_storage_led.api import VOICE_AGENT_TOOLS, create_app
from searchable_storage_led.auth import AuthIdentity
from searchable_storage_led.config import (
    AuthConfig,
    EdgeConfig,
    LedConfig,
    LlmConfig,
    LlmEndpoint,
    StorageConfig,
    ZebraConfig,
)
from searchable_storage_led.edge import SqliteEdgeRepository
from searchable_storage_led.interaction_contract import VOICE_SCOPE_MESSAGE
from searchable_storage_led.locations import Location, LocationStore
from searchable_storage_led.llm_client import LlmError
from searchable_storage_led.repositories import StorageRepositories
from searchable_storage_led.store import InventoryStore


def _insert_legacy_location(
    database_path, *, location_id: str, location_type: str, label: str
) -> None:
    """Create an invalid pre-uniqueness location fixture."""
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO locations(id, type, label) VALUES (?, ?, ?)",
            (location_id, location_type, label),
        )


class MeasurementLlm:
    def __init__(self) -> None:
        self.call = None

    def complete_json(self, *, system, user_text, images=None):
        self.call = {"system": system, "user_text": user_text, "images": images}
        return {
            "reference_detected": True,
            "bounds_mm": {"left_mm": 10, "top_mm": 20, "right_mm": 84.8, "bottom_mm": 38.2},
            "dimensions": {"length_mm": 74.8, "width_mm": 18.2},
            "confidence": 0.91,
            "reason": "Reference corners and item edges are visible.",
        }


class ProviderCooldownLlm:
    def provider_failures(self):
        return [
            {
                "provider": "openai",
                "model": "gpt-test",
                "category": "rate_limited",
                "status_code": 429,
                "retry_in_seconds": 42,
                "message": "must not be exposed",
            }
        ]


def test_health_and_build_expose_build_metadata(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [{"size": "small", "bin_count": 1, "spans": [1]}],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    health = client.get("/health").json()
    ready_response = client.get("/ready")
    ready = ready_response.json()
    build = client.get("/build").json()

    assert ready_response.status_code == 200
    assert ready["schema"] == "magpie.readiness.v2"
    assert ready["ready"] is True
    assert ready["storage_probe"] == {
        "ok": True,
        "backend": "sqlite",
        "workspace_id": "local",
    }
    assert ready["operational_state_probe"] == {"ok": True}
    assert ready["agent_probe"] == {
        "ok": True,
        "required": False,
        "provider": "openrouter",
        "model": "google/gemini-3.1-flash-lite",
    }
    assert ready["runtime"] == {
        "schema": "magpie.runtime.v1",
        "role": "cabinet_service",
        "hardware_owner": True,
        "storage_authority": "sqlite",
        "workspace_scope": "fixed",
        "edge_execution": "local",
        "edge_queue_enabled": False,
    }
    assert health["version"]
    assert health["build"]["version"] == health["version"]
    assert health["build_label"]
    assert health["auth"]["workspace_id"] == "local"
    assert health["agent"] == {
        "ready": False,
        "provider": "openrouter",
        "model": "google/gemini-3.1-flash-lite",
        "api": "chat_completions",
        "effective_backend": "deterministic",
    }
    assert build["version"] == health["version"]
    assert build["label"] == health["build_label"]


def test_hosted_browser_access_is_limited_to_the_public_origin(
    tmp_path,
    monkeypatch,
) -> None:
    qr_values: list[str] = []

    def fake_qr(value: str) -> str:
        qr_values.append(value)
        return "<svg data-test-qr='true'></svg>"

    monkeypatch.setattr("searchable_storage_led.api.render_qr_svg", fake_qr)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
                public_origin="https://magpie.example.com",
            )
        )
    )

    allowed = client.options(
        "/health",
        headers={
            "origin": "https://magpie.example.com",
            "access-control-request-method": "GET",
        },
    )
    denied = client.options(
        "/health",
        headers={
            "origin": "https://unrelated.example",
            "access-control-request-method": "GET",
        },
    )

    assert allowed.status_code == 200
    assert (
        allowed.headers["access-control-allow-origin"]
        == "https://magpie.example.com"
    )
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers

    kiosk = client.get("/kiosk")
    assert kiosk.status_code == 200
    assert (
        'data-handoff-url="https://magpie.example.com/storage?view=places"'
        in kiosk.text
    )
    qr_prefix = 'src="data:image/svg+xml;base64,'
    encoded_qr = kiosk.text.split(qr_prefix, 1)[1].split('"', 1)[0]
    assert b"<svg" in base64.b64decode(encoded_qr)
    assert qr_values == ["https://magpie.example.com/storage?view=places"]
    assert "__MAGPIE_STORAGE_HANDOFF" not in kiosk.text


def test_local_browser_tools_retain_cross_origin_access(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )

    response = client.options(
        "/health",
        headers={
            "origin": "http://127.0.0.1:9000",
            "access-control-request-method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"


def test_readiness_fails_closed_without_changing_liveness(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=0,
        backend="none",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    repositories = StorageRepositories.sqlite(tmp_path / "injected.db")

    def fail_probe() -> None:
        raise RuntimeError("secret backend connection detail")

    repositories.locations.health_probe = fail_probe
    client = TestClient(create_app(cfg, repositories=repositories))

    assert client.get("/health").status_code == 200
    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["ok"] is False
    assert response.json()["ready"] is False
    assert response.json()["storage_probe"] == {
        "backend": "sqlite",
        "workspace_id": "local",
        "ok": False,
        "error": "RuntimeError",
    }
    assert "secret backend connection detail" not in response.text


def test_readiness_fails_closed_when_operational_state_is_not_writable(
    tmp_path,
    monkeypatch,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=0,
        backend="none",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "state" / "inventory.db"),
    )
    repositories = StorageRepositories.sqlite(tmp_path / "injected.db")

    def fail_state_probe(*args, **kwargs):
        raise PermissionError("secret operational path")

    monkeypatch.setattr("searchable_storage_led.api.tempfile.mkstemp", fail_state_probe)
    client = TestClient(create_app(cfg, repositories=repositories))

    assert client.get("/health").status_code == 200
    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["ok"] is False
    assert response.json()["ready"] is False
    assert response.json()["storage_probe"]["ok"] is True
    assert response.json()["operational_state_probe"] == {
        "ok": False,
        "error": "PermissionError",
    }
    assert "secret operational path" not in response.text


def _hosted_config_client(
    tmp_path,
    *,
    edge_mode: str = "workspace",
    reported_storage_backend: str = "sqlite",
) -> TestClient:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=0,
        backend="none",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "state" / "inventory.db"),
        storage=StorageConfig(
            backend="supabase",
            supabase_url="https://project.supabase.co",
            workspace_id="",
        ),
        auth=AuthConfig(
            mode="supabase",
            supabase_url="https://project.supabase.co",
            multi_workspace=True,
        ),
        edge=EdgeConfig(mode=edge_mode),
    ).validated()
    local_repositories = StorageRepositories.sqlite(tmp_path / "injected.db")
    repositories = StorageRepositories(
        inventory=local_repositories.inventory,
        locations=local_repositories.locations,
        backend=reported_storage_backend,
        workspace_id=(
            "request-scoped"
            if reported_storage_backend == "supabase"
            else local_repositories.workspace_id
        ),
    )
    return TestClient(
        create_app(
            cfg,
            repositories=repositories,
            edge_repository=SimpleNamespace(status=lambda: {"devices": []}),
            auth_service=SimpleNamespace(
                authenticate=lambda *args, **kwargs: AuthIdentity(
                    "test-user",
                    "test@example.com",
                    "request-scoped",
                    "owner",
                ),
                close=lambda: None,
            ),
            workspace_member_router=SimpleNamespace(close=lambda: None),
            workspace_catalog_service=object(),
            agent_credential_service=object(),
        )
    )


def test_central_service_readiness_requires_a_configured_agent(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    response = _hosted_config_client(tmp_path).get("/ready")

    assert response.status_code == 503
    readiness = response.json()
    assert readiness["ready"] is False
    assert readiness["runtime"]["role"] == "central_service"
    assert readiness["storage_probe"]["ok"] is True
    assert readiness["operational_state_probe"]["ok"] is True
    assert readiness["agent_probe"] == {
        "ok": False,
        "required": True,
        "provider": "openrouter",
        "model": "google/gemini-3.1-flash-lite",
    }


def test_central_service_readiness_accepts_a_configured_agent(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "configured")

    response = _hosted_config_client(tmp_path).get("/ready")

    assert response.status_code == 200
    assert response.json()["agent_probe"] == {
        "ok": True,
        "required": True,
        "provider": "openrouter",
        "model": "google/gemini-3.1-flash-lite",
    }


def test_capabilities_describe_client_feature_surface(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [{"size": "small", "bin_count": 1, "spans": [1]}],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    response = client.get("/capabilities")

    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "magpie.capabilities.v1"
    assert body["build"]["version"]
    assert body["interaction"] == {
        "schema": "magpie.interaction_contract.v1",
        "voice": {
            "primary_actions": ["find", "put_away"],
            "put_away_destination_policy": "existing_places_only",
            "follow_up_actions": [
                "confirm",
                "cancel",
                "correct_pending",
                "undo",
                "clear_lights",
            ],
            "place_management": False,
            "place_configuration_handoff": "/storage?view=places",
            "out_of_scope_message": (
                "Voice is for finding and putting away items. "
                "Use the Storage screen to create or manage places, rules, and other changes."
            ),
        },
        "visual": {
            "place_management": True,
            "place_actions": [
                "create",
                "edit",
                "move",
                "archive",
                "restore",
                "label",
                "bind_hardware",
            ],
            "places_path": "/storage?view=places",
        },
    }
    assert body["features"]["agent_chat"] is True
    assert body["features"]["client_bootstrap"] is True
    assert body["features"]["runtime_readiness"] is True
    assert body["features"]["inventory_catalog_pagination"] is True
    assert body["endpoints"]["readiness"] == "/ready"
    assert body["endpoints"]["inventory_catalog"] == "/inventory/catalog"
    assert body["features"]["agent_location_inspection"] is True
    assert body["features"]["agent_storage_roots"] is True
    assert body["features"]["agent_label_batch_preview"] is True
    assert body["features"]["agent_attach_photo_undo"] is True
    assert body["features"]["agent_bulk_reorganization"] is True
    assert body["features"]["agent_remove_item"] is True
    assert body["features"]["agent_remove_item_undo"] is True
    assert body["features"]["agent_update_item_undo"] is True
    assert body["features"]["agent_update_item_size_undo"] is True
    assert body["features"]["agent_update_item_home_undo"] is True
    assert body["features"]["agent_update_location_undo"] is True
    assert body["features"]["agent_update_rule_undo"] is True
    assert body["features"]["agent_update_size_rule_undo"] is True
    assert body["features"]["learned_rule_conflicts"] is True
    assert body["features"]["inventory_json_import"] is True
    assert body["features"]["inventory_csv_import"] is True
    assert body["features"]["learning_examples_review"] is True
    assert body["features"]["learning_examples_summary"] is True
    assert body["features"]["learning_examples_approve"] is True
    assert body["features"]["learning_examples_bulk_approve"] is True
    assert body["features"]["learning_examples_reject"] is True
    assert body["features"]["learning_examples_promote"] is True
    assert body["features"]["learning_rule_adherence"] is True
    assert body["features"]["learning_undo"] is True
    assert body["features"]["learned_aliases"] is True
    assert body["features"]["item_homes"] is True
    assert body["features"]["manual_item_home_updates"] is True
    assert body["features"]["item_metadata_updates"] is True
    assert body["features"]["item_qr_scan"] is True
    assert body["features"]["item_label_pdfs"] is True
    assert body["features"]["item_history"] is True
    assert body["features"]["location_history"] is True
    assert body["features"]["item_size_explain"] is True
    assert body["features"]["natural_size_corrections"] is True
    assert body["features"]["photo_sort_review_flags"] is True
    assert body["features"]["photo_attachment_retry"] is True
    assert body["features"]["browser_photo_normalization"] is True
    assert body["features"]["calibrated_photo_measurement"] is True
    assert body["features"]["check_station"] is True
    assert body["features"]["measured_size_provenance"] is True
    assert body["endpoints"]["check_station"] == "/check-station"
    assert body["features"]["pwa_manifest"] is True
    assert body["features"]["qr_scan_local_fallback"] is True
    assert body["features"]["qr_scan_page_cache"] is True
    assert body["features"]["inventory_move_preview"] is True
    assert body["features"]["container_move"] is True
    assert body["features"]["atomic_location_creation"] is True
    assert body["features"]["atomic_location_lifecycle"] is True
    assert body["features"]["guided_location_audits"] is True
    assert body["features"]["location_content_revisions"] is True
    assert body["limits"]["location_audit_placements_max"] == 100
    assert body["endpoints"]["location_audit"] == "/locations/{location_id}/audit"
    assert (
        body["endpoints"]["verify_location_contents"]
        == "/locations/{location_id}/verify"
    )
    assert body["endpoints"]["preview_inventory_move"] == "/inventory/move-preview"
    assert (
        body["endpoints"]["preview_location_move"]
        == "/locations/{location_id}/move-preview"
    )
    assert body["features"]["scan_quick_put"] is True
    assert body["features"]["scan_quick_put_retry_queue"] is True
    assert body["features"]["scan_quick_put_photo_payload_queue"] is True
    assert body["features"]["scan_quick_put_workspace_queue"] is True
    assert body["features"]["scan_quick_put_idempotency"] is True
    assert body["features"]["mobile_offline_mutation_queue"] is True
    assert body["features"]["assistant_chat_idempotency"] is True
    assert body["features"]["agent_usage_metering"] is True
    assert body["features"]["storage_usage_metering"] is True
    assert body["features"]["workspace_readiness"] is True
    assert body["limits"]["photo_upload_bytes_max"] == 10 * 1024 * 1024
    assert body["limits"]["browser_photo_edge_pixels_max"] == 2048
    assert body["limits"]["offline_photo_queue_bytes_max"] == 18 * 1024 * 1024
    assert body["features"]["scan_quick_put_inline_edit"] is True
    assert body["features"]["scan_quick_put_interaction"] is True
    assert body["features"]["manual_inventory_interactions"] is True
    assert body["features"]["supabase_schema"] is True
    assert body["features"]["hardware_bindings"] is True
    assert body["features"]["hardware_binding_commissioning"] is True
    assert body["features"]["atomic_hardware_bindings"] is True
    assert body["features"]["atomic_inventory_import"] is True
    assert body["features"]["hardware_binding_validation"] is True
    assert body["features"]["hardware_diagnostics"] is True
    assert (
        body["endpoints"]["preview_hardware_commissioning"]
        == "/locations/{location_id}/hardware-bindings/batch-preview"
    )
    assert (
        body["endpoints"]["apply_hardware_commissioning"]
        == "/locations/{location_id}/hardware-bindings/batch"
    )
    assert body["features"]["edge_long_poll"] is False
    assert body["features"]["edge_local_capability_discovery"] is True
    assert body["features"]["voice_interaction_link"] is True
    assert body["features"]["voice_tuning_summary"] is True
    assert body["features"]["voice_acceptance"] is True
    assert body["features"]["voice_threshold_calibration"] is True
    assert body["features"]["voice_activity_detection"] is True
    assert body["features"]["voice_local_wake"] is True
    assert body["features"]["voice_live_status"] is True
    assert body["features"]["screen_heartbeat"] is True
    assert body["features"]["kiosk_touch_actions"] is True
    assert body["features"]["inventory_csv_export"] is True
    assert body["features"]["inventory_pdf_export"] is True
    assert body["endpoints"]["export_inventory_pdf"] == "/export/inventory.pdf"
    assert body["features"]["printable_label_pdfs"] is True
    assert body["limits"]["edge_claim_wait_seconds_max"] == 25
    assert body["endpoints"]["photo_measurement"] == "/measurement/photo"
    assert body["endpoints"]["measurement_grid_pdf"] == "/measurement/calibration-grid.pdf"
    assert body["features"]["lights"] is True
    assert body["endpoints"]["assistant_chat"] == "/assistant/chat"
    assert body["endpoints"]["agent_usage"] == "/usage/agent"
    assert body["endpoints"]["storage_usage"] == "/usage/storage"
    assert body["endpoints"]["mcp"] == "/mcp/"
    assert body["endpoints"]["workspace_agent_credentials"] == "/workspace/agent-credentials"
    assert body["features"]["agent_responses_api"] is True
    assert body["features"]["agent_conversation_history"] is True
    assert body["features"]["agent_inventory_review"] is True
    assert body["features"]["find_not_here_correction"] is True
    assert body["features"]["voice_putaway_ui_handoff"] is True
    assert body["features"]["canonical_ask_surface"] is True
    assert body["features"]["cabinet_console"] is True
    assert body["features"]["mcp_server"] is True
    assert body["features"]["workspace_agent_credentials"] is False
    assert body["endpoints"]["assistant_learning_examples"] == "/assistant/learning/examples"
    assert body["endpoints"]["assistant_learning_summary"] == "/assistant/learning/summary"
    assert body["endpoints"]["approve_learning_example"] == "/assistant/learning/examples/{example_id}/approve"
    assert body["endpoints"]["bulk_approve_learning_examples"] == "/assistant/learning/examples/bulk-approve"
    assert body["endpoints"]["reject_learning_example"] == "/assistant/learning/examples/{example_id}"
    assert (
        body["endpoints"]["promote_learning_example"]
        == "/assistant/learning/examples/{example_id}/promote"
    )
    assert body["endpoints"]["learning_rule_adherence"] == "/assistant/learning/adherence"
    assert body["endpoints"]["placement_size"] == "/placement/size"
    assert body["endpoints"]["photo_attachment_retry"] == "/photos/attach-retry"
    assert body["endpoints"]["assistant_followup"] == "/assistant/followup"
    assert body["endpoints"]["assistant_find_not_here"] == "/assistant/find/not-here"
    assert (
        body["endpoints"]["assistant_handoff"]
        == "/assistant/handoffs/{interaction_id}"
    )
    assert body["endpoints"]["move_location"] == "/locations/{location_id}/move"
    assert body["endpoints"]["location_hardware_bindings"] == "/locations/{location_id}/hardware-bindings"
    assert body["endpoints"]["hardware_diagnostics"] == "/hardware/diagnostics"
    assert body["endpoints"]["scan_quick_put"] == "/locations/{location_id}/quick-put"
    assert body["endpoints"]["workspace_location_scan"] == "/scan/{workspace_id}/l/{location_id}"
    assert body["endpoints"]["workspace_item_scan"] == "/scan/{workspace_id}/i/{item_id}"
    assert body["endpoints"]["item_scan"] == "/i/{item_id}"
    assert body["endpoints"]["item_label_pdf"] == "/items/{item_id}/label.pdf"
    assert body["features"]["item_label_batch_pdfs"] is True
    assert body["endpoints"]["item_label_batch_pdf"] == "/items/labels.pdf"
    assert body["features"]["location_label_batch_pdfs"] is True
    assert (
        body["endpoints"]["location_label_batch_pdf"]
        == "/locations/labels.pdf"
    )
    assert body["features"]["inventory_lifecycle_preview"] is True
    assert body["features"]["inventory_lifecycle_bulk"] is True
    assert (
        body["endpoints"]["preview_inventory_lifecycle"]
        == "/inventory/lifecycle/preview"
    )
    assert (
        body["endpoints"]["apply_inventory_lifecycle"]
        == "/inventory/lifecycle/apply"
    )
    assert (
        body["endpoints"]["location_qr_svg"]
        == "/locations/{location_id}/qr.svg"
    )
    assert (
        body["endpoints"]["location_label_pdf"]
        == "/locations/{location_id}/label.pdf"
    )
    assert body["endpoints"]["qr_decoder"] == "/jsqr.min.js"
    assert body["endpoints"]["item_history"] == "/items/{item_id}/history"
    assert body["endpoints"]["update_item_home"] == "/inventory/{item_id}/home"
    assert body["endpoints"]["inventory_review"] == "/inventory/review"
    assert body["endpoints"]["mark_inventory_reviewed"] == "/inventory/reviewed"
    assert body["endpoints"]["location_history"] == "/locations/{location_id}/history"
    assert body["endpoints"]["voice_tuning"] == "/voice/tuning"
    assert body["endpoints"]["voice_acceptance"] == "/voice/acceptance"
    assert body["endpoints"]["voice_calibration"] == "/voice/calibration"
    assert body["endpoints"]["voice_status"] == "/voice/status"
    assert body["endpoints"]["voice_state"] == "/voice/state"
    assert body["endpoints"]["kiosk_state"] == "/kiosk/state"
    assert body["endpoints"]["kiosk_confirm"] == "/kiosk/actions/confirm"
    assert body["endpoints"]["kiosk_revise"] == "/kiosk/actions/revise"
    assert body["endpoints"]["kiosk_cancel"] == "/kiosk/actions/cancel"
    assert body["endpoints"]["kiosk_undo"] == "/kiosk/actions/undo"
    assert body["endpoints"]["kiosk_clear"] == "/kiosk/actions/clear"
    assert body["features"]["mobile_client"] is True
    assert body["endpoints"]["mobile_client"] == "/mobile"
    assert body["endpoints"]["cabinet_console"] == "/cabinet"
    assert body["endpoints"]["location_contents"] == "/locations/{location_id}/contents"
    assert body["endpoints"]["create_location_structure"] == "/locations/structure"
    assert (
        body["endpoints"]["create_location_children_batch"]
        == "/locations/{location_id}/children/batch"
    )
    assert body["endpoints"]["screen_heartbeat"] == "/screen/heartbeat"
    assert body["endpoints"]["edge_device_status"] == (
        "/edge/devices/{device_id}/status"
    )
    assert body["endpoints"]["edge_local_capabilities"] == (
        "/edge/local-capabilities"
    )
    assert body["endpoints"]["screen_status"] == "/screen/status"
    assert body["endpoints"]["demo_readiness"] == "/demo/readiness"
    assert body["endpoints"]["workspace_readiness"] == "/workspace/readiness"
    assert body["endpoints"]["product_readiness"] == "/product/readiness"
    assert body["endpoints"]["client_bootstrap"] == "/client/bootstrap"
    assert body["endpoints"]["delete_account"] == "/account"
    assert body["endpoints"]["rename_workspace"] == "/workspace"
    assert body["endpoints"]["leave_workspace"] == "/workspace/membership"
    assert body["endpoints"]["delete_workspace"] == "/workspace"
    assert body["features"]["workspace_rename"] is False
    assert body["features"]["workspace_leave"] is False
    assert body["features"]["workspace_deletion"] is False
    assert body["features"]["account_deletion"] is False
    assert body["endpoints"]["label_sheet_pdf"] == "/locations/{location_id}/labels.pdf"
    assert body["endpoints"]["export_inventory_csv"] == "/export/inventory.csv"
    assert body["endpoints"]["import_inventory_json"] == "/import/inventory.json"
    assert body["compatibility_endpoints"]["assistant_ask"] == {
        "path": "/assistant/ask",
        "replacement": "/assistant/chat",
        "mode": "ask",
    }
    assert body["compatibility_endpoints"]["assistant_find"] == {
        "path": "/assistant/find",
        "replacement": "/assistant/chat",
        "mode": "find",
    }
    assert body["compatibility_endpoints"]["assistant_agent"]["replacement"] == "/assistant/chat"
    assert body["compatibility_endpoints"]["assistant_command"]["mode"] == "command"

    bootstrap = client.get("/client/bootstrap")
    assert bootstrap.status_code == 200
    startup = bootstrap.json()
    assert startup["schema"] == "magpie.client_bootstrap.v1"
    assert startup["auth"] == {"mode": "none", "multi_workspace": False}
    assert startup["identity"] is None
    assert startup["workspace"] is None
    assert startup["workspaces"] == []
    assert startup["capabilities"]["schema"] == "magpie.capabilities.v1"


def test_agent_usage_summarizes_recorded_interactions_with_a_bounded_window(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]}
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="none",
        layout_path=str(layout_path),
        database_path=str(database_path),
    )
    store = InventoryStore(database_path)
    store.record_assistant_interaction(
        route="/assistant/chat",
        command="old unmetered interaction",
        response={"action": "answered"},
    )
    for index in range(2):
        store.record_assistant_interaction(
            route="/assistant/chat",
            command=f"metered {index}",
            response={
                "agent": {
                    "provider": "openai",
                    "model": "gpt-test",
                    "api": "responses",
                    "usage": {
                        "requests": index + 1,
                        "input_tokens": 100 + index,
                        "cached_input_tokens": 50,
                        "output_tokens": 20,
                        "reasoning_tokens": 5,
                        "total_tokens": 120 + index,
                    },
                }
            },
        )
    store.record_assistant_interaction(
        route="/assistant/chat",
        command="failed request",
        response={
            "ok": False,
            "provider": "openai",
            "model": "gpt-test",
            "failure_category": "rate_limited",
            "provider_status": 429,
        },
        status="failed",
    )

    response = TestClient(create_app(cfg, llm=ProviderCooldownLlm())).get(
        "/usage/agent?limit=3"
    )
    body = response.json()

    assert response.headers["cache-control"] == "no-store"
    assert body["schema"] == "magpie.agent_usage.v2"
    assert body["window"]["interaction_count"] == 3
    assert body["window"]["metered_interactions"] == 2
    assert body["window"]["may_be_truncated"] is True
    assert body["usage"]["requests"] == 3
    assert body["usage"]["input_tokens"] == 201
    assert body["usage"]["total_tokens"] == 241
    assert body["routing"] == {
        "assistant_interactions": 3,
        "deterministic_interactions": 0,
        "model_interactions": 3,
        "model_interaction_rate": 1.0,
    }
    assert body["models"][0]["model"] == "gpt-test"
    assert body["models"][0]["interactions"] == 2
    assert body["reliability"] == {
        "successful_interactions": 2,
        "failed_interactions": 1,
        "recorded_outcomes": 3,
        "failure_rate": 0.333,
        "degraded": True,
        "current_provider_failures": [
            {
                "provider": "openai",
                "model": "gpt-test",
                "category": "rate_limited",
                "status_code": 429,
                "retry_in_seconds": 42,
            }
        ],
    }
    assert body["failures"][0]["category"] == "rate_limited"
    assert body["failures"][0]["count"] == 1
    assert body["failures"][0]["status_codes"] == [429]
    assert "message" not in body["reliability"]["current_provider_failures"][0]

    complete_window = TestClient(create_app(cfg, llm=ProviderCooldownLlm())).get(
        "/usage/agent?limit=4"
    ).json()
    assert complete_window["routing"] == {
        "assistant_interactions": 4,
        "deterministic_interactions": 1,
        "model_interactions": 3,
        "model_interaction_rate": 0.75,
    }


def test_export_inventory_csv_returns_human_editable_inventory(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(db_path)
    item_id = store.upsert_item(
        name="M4 T-nuts",
        description="hammer-slot fasteners",
        category="hardware",
        aliases=["tee nuts", "tnuts"],
    )
    store.update_item_size(
        item_id=item_id,
        size_class="tiny",
        source="user",
        dimensions={"length_mm": 8, "width_mm": 8},
    )
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=2, unit="each")

    response = client.get("/export/inventory.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'filename="magpie-inventory.csv"' in response.headers["content-disposition"]
    rows = list(csv.DictReader(response.text.splitlines()))
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "M4 T-nuts"
    assert row["location_id"] == "B001"
    assert row["location_path"] == "Test > B001"
    assert row["location_path_types"] == "cabinet > bin"
    assert row["quantity"] == "2.0"
    assert row["unit"] == "each"
    assert row["category"] == "hardware"
    assert row["description"] == "hammer-slot fasteners"
    assert row["aliases"] == "tee nuts; tnuts"
    assert row["needs_review"] == "false"
    assert row["size_class"] == "tiny"
    assert row["size_source"] == "user"
    assert json.loads(row["dimensions"]) == {"length_mm": 8, "width_mm": 8}


def test_assistant_command_can_carry_uploaded_photo(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    db_path = tmp_path / "inventory.db"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))

    photo_response = client.post(
        "/photos",
        json={
            "filename": "part.jpg",
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(b"image").decode("ascii"),
        },
    )
    assert photo_response.status_code == 200
    photo_id = photo_response.json()["photo"]["id"]

    command_response = client.post(
        "/assistant/command",
        json={"text": "put this cable away", "photo_ids": [photo_id]},
    )

    assert command_response.status_code == 200
    assert command_response.json()["assistant_source"] == "deterministic"
    assert command_response.json()["automation"]["auto_saved"] is True
    draft = command_response.json()["decision"]["item_draft"]
    assert draft["bin_id"] == "B001"
    assert draft["photo_ids"] == [photo_id]

    review_response = client.get("/inventory/review")
    assert review_response.status_code == 200
    review_item = review_response.json()["items"][0]
    assert review_item["name"] == "Cable"
    assert review_item["bin_id"] == "B001"
    assert review_item["needs_review"] is True
    assert review_item["review_reason"] == "Agent marked this item for review."

    reviewed_response = client.post(
        "/inventory/reviewed",
        json={"item_id": review_item["item_id"], "bin_id": review_item["bin_id"]},
    )
    assert reviewed_response.status_code == 200
    assert reviewed_response.json()["approved"]["name"] == "Cable"
    assert reviewed_response.json()["approved"]["review_reason"] == "Agent marked this item for review."
    assert client.get("/inventory/review").json()["items"] == []
    example = InventoryStore(db_path).recent_examples(limit=1)[0]
    assert example["kind"] == "review_approved"
    assert example["command"] == "review approved Cable in B001"
    assert example["review_status"] == "approved"
    payload = json.loads(example["action"])
    assert payload["approved_review"]["name"] == "Cable"
    assert payload["approved_review"]["review_reason"] == "Agent marked this item for review."


def test_assistant_ask_records_interaction(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    db_path = tmp_path / "inventory.db"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/ask",
        json={"query": "blue pry tools", "photo_ids": []},
    )

    assert response.status_code == 200
    with InventoryStore(db_path).connect() as conn:
        row = conn.execute(
            """
            SELECT route, command, photo_ids, response_json
            FROM assistant_interactions
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    assert row["route"] == "/assistant/ask"
    assert row["command"] == "blue pry tools"
    assert row["photo_ids"] == ""
    assert json.loads(row["response_json"])["action"] == response.json()["action"]


def test_assistant_interactions_returns_latest_and_after_cursor(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    client.post("/assistant/ask", json={"query": "blue pry tools"})
    client.post("/assistant/ask", json={"query": "M4 T-nuts"})

    latest = client.get("/assistant/interactions?limit=1").json()["interactions"]
    assert len(latest) == 1
    assert latest[0]["command"] == "M4 T-nuts"

    after = client.get(f"/assistant/interactions?after_id={latest[0]['id'] - 1}").json()
    assert [row["command"] for row in after["interactions"]] == ["M4 T-nuts"]


def test_assistant_find_records_interaction_for_voice_screen_bridge(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post("/inventory", json={"name": "solder wick", "bin_id": "B001"})

    response = client.post("/assistant/find", json={"query": "solder wick"})

    assert response.status_code == 200
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert latest["route"] == "/assistant/find"
    assert latest["command"] == "solder wick"
    assert latest["response"]["results"][0]["name"] == "solder wick"


def test_find_surfaces_physical_path_for_nested_locations(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    locations = LocationStore(db_path)
    locations.upsert(Location(id="solder-pouch", type="pouch", label="Solder Pouch", parent_id="B001"))
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="solder wick")
    store.put_inventory(
        item_id=item_id,
        bin_id="solder-pouch",
        quantity=1,
        unit="roll",
        normalize_bin=False,
    )

    found = client.post("/assistant/find", json={"query": "solder wick"}).json()
    plain_search = client.get("/search?q=solder%20wick").json()
    chat = client.post("/assistant/chat", json={"text": "where is solder wick"}).json()

    for result in (found["results"][0], plain_search["results"][0]):
        assert result["item_id"] == item_id
        assert result["location_id"] == "solder-pouch"
        assert result["bin_id"] == "solder-pouch"
        assert [node["id"] for node in result["path"]] == [
            "main-cabinet",
            "B001",
            "solder-pouch",
        ]
        assert result["lit_location"] == "B001"
    assert found["message"] == "solder wick is in Test > B001 > Solder Pouch."
    assert chat["mode"] == "find"
    assert chat["message"] == "solder wick is in Test > B001 > Solder Pouch."


def test_assistant_chat_find_uses_canonical_route(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post("/inventory", json={"name": "solder wick", "bin_id": "B001"})

    response = client.post("/assistant/chat", json={"text": "find solder wick"})

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "find"
    assert body["results"][0]["name"] == "solder wick"
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert latest["route"] == "/assistant/chat"
    assert latest["response"]["action"] == "found"


def test_voice_auto_find_stays_on_fast_path_when_openai_is_configured(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "configured")
    monkeypatch.setattr(
        "searchable_storage_led.api.run_agent",
        lambda *args, **kwargs: pytest.fail("routine voice find called the agent"),
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]}
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    client.post("/inventory", json={"name": "USB drive", "bin_id": "B001"})

    response = client.post(
        "/assistant/chat",
        json={"text": "Magpie, find USB drive", "source": "voice"},
    )

    assert response.status_code == 200
    assert response.json()["mode"] == "find"
    assert response.json()["results"][0]["name"] == "USB drive"


def test_agent_rate_limit_is_recorded_and_returned_as_unavailable(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "searchable_storage_led.api.run_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            LlmError("Responses API request failed (429): rate limit exceeded")
        ),
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )

    response = client.post(
        "/assistant/chat",
        json={"text": "move all wrenches into a large bin"},
    )

    assert response.status_code == 503
    assert "temporarily rate limited" in response.text
    failure = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert failure["status"] == "failed"
    assert failure["response"]["action"] == "model_rate_limited"
    assert failure["response"]["failure_category"] == "rate_limited"
    assert failure["response"]["provider_status"] == 429
    assert failure["response"]["model"] == "google/gemini-3.1-flash-lite"
    assert response.headers["x-magpie-interaction-id"] == str(failure["id"])
    assert response.headers["x-magpie-error-action"] == "model_rate_limited"
    assert response.headers["x-magpie-failure-category"] == "rate_limited"


@pytest.mark.parametrize(
    ("category", "status_code", "action", "message"),
    [
        (
            "authentication",
            401,
            "model_authentication_error",
            "credentials need attention",
        ),
        (
            "quota_exhausted",
            402,
            "model_quota_exhausted",
            "no available credits",
        ),
        (
            "provider_unavailable",
            503,
            "model_unavailable",
            "temporarily unavailable",
        ),
    ],
)
def test_agent_provider_failures_are_sanitized_and_classified(
    tmp_path, monkeypatch, category, status_code, action, message
) -> None:
    monkeypatch.setattr(
        "searchable_storage_led.api.run_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            LlmError(
                "secret upstream provider response",
                category=category,
                status_code=status_code,
            )
        ),
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )

    response = client.post(
        "/assistant/chat", json={"text": "move all wrenches into a large bin"}
    )

    assert response.status_code == 503
    assert message in response.text
    assert "secret upstream" not in response.text
    failure = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert failure["response"]["action"] == action
    assert failure["response"]["failure_category"] == category
    assert failure["response"]["provider_status"] == status_code
    assert response.headers["x-magpie-error-action"] == action
    assert response.headers["x-magpie-failure-category"] == category


def test_auto_agent_prefers_cheap_provider_and_uses_openai_when_it_is_only_key(
    tmp_path, monkeypatch
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "searchable_storage_led.api.run_agent",
        lambda *args, **kwargs: AgentResult(message="Ready."),
    )

    monkeypatch.setenv("OPENAI_API_KEY", "openai")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    openai_client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "openai.db"),
            )
        )
    )
    openai_response = openai_client.post(
        "/assistant/chat", json={"text": "organize all wrenches"}
    ).json()

    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter")
    cheap_client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "cheap.db"),
            )
        )
    )
    cheap_response = cheap_client.post(
        "/assistant/chat", json={"text": "organize all wrenches"}
    ).json()

    assert openai_response["agent"] == {
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "api": "responses",
        "usage": {},
    }
    assert cheap_response["agent"] == {
        "provider": "openrouter",
        "model": "google/gemini-3.1-flash-lite",
        "api": "chat_completions",
        "usage": {},
    }


def test_assistant_chat_lists_storage_roots_without_proposing_putaway(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "searchable_storage_led.api.run_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("storage discovery should not call the planning model")
        ),
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="none",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post(
        "/locations",
        json={"id": "attic", "type": "room", "label": "Attic"},
    )
    client.post(
        "/locations",
        json={"id": "garage", "type": "room", "label": "Garage"},
    )

    for command in (
        "What storage spaces do I have?",
        "List my storage places",
        "List storage",
        "Show me all my storage",
        "What places have I added?",
        "Which storage locations are in Magpie?",
    ):
        response = client.post(
            "/assistant/chat",
            json={"text": command, "mode": "ask"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "agent"
        assert body["action"] == "answered"
        assert body["plan"] == []
        assert "Attic" in body["message"]
        assert "Garage" in body["message"]
        assert body["trace"][0]["tool"] == "list_storage_roots"
        assert {root["id"] for root in body["trace"][0]["result"]} == {
            "attic",
            "garage",
        }

    voice = client.post(
        "/assistant/chat",
        json={
            "text": "Where are my storage locations?",
            "mode": "ask",
            "source": "voice",
        },
    ).json()
    assert voice["mode"] == "clarify"
    assert voice["decision"]["action"] == "voice_scope_ui_required"


@pytest.mark.parametrize(
    ("command", "expected_arguments"),
    [
        ("Add a Garage", {"type": "area", "label": "Garage"}),
        ("Create a North Garage", {"type": "room", "label": "North Garage"}),
        ("Create a Hall Closet", {"type": "closet", "label": "Hall Closet"}),
        (
            "Create a Main Cabinet",
            {"type": "cabinet", "label": "Main Cabinet"},
        ),
    ],
)
def test_assistant_chat_recognizes_natural_first_storage_phrases(
    tmp_path, monkeypatch, command, expected_arguments
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "software-only", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )

    response = client.post("/assistant/chat", json={"text": command})

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "proposed"
    assert body["plan"] == [
        {"tool": "create_location", "arguments": expected_arguments}
    ]


def test_assistant_chat_replays_client_request_without_duplicate_proposal(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "software-only", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    payload = {
        "text": "Create an Offline Test garage",
        "client_request_id": "offline-request-123",
    }

    first = client.post("/assistant/chat", json=payload)
    replay = client.post("/assistant/chat", json=payload)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == {**first.json(), "idempotent_replay": True}
    assert first.json()["client_request_id"] == payload["client_request_id"]
    interactions = client.get("/assistant/interactions?limit=20").json()["interactions"]
    matching = [row for row in interactions if row["command"] == payload["text"]]
    assert len(matching) == 1
    assert matching[0]["response"]["client_request_id"] == payload["client_request_id"]
    invalid = client.post(
        "/assistant/chat",
        json={"text": "Add a Garage", "client_request_id": "not allowed / value"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "client_request_id is invalid"


def test_assistant_chat_setup_prompt_proposes_multiple_top_level_places(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "software-only", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )

    incomplete = client.post(
        "/assistant/chat",
        json={"text": "Set up my storage. I have "},
    ).json()
    assert incomplete["mode"] == "clarify"
    assert incomplete["decision"]["action"] == "clarify"
    assert incomplete["decision"]["plan"] == []

    proposed = client.post(
        "/assistant/chat",
        json={
            "text": (
                "Set up my storage. I have an attic, a garage, "
                "and a printer cabinet"
            )
        },
    )

    assert proposed.status_code == 200
    body = proposed.json()
    assert body["mode"] == "agent"
    assert body["action"] == "proposed"
    assert body["message"] == (
        "Create Attic, Garage, and Printer Cabinet as top-level storage places."
    )
    assert body["plan"] == [
        {
            "tool": "create_location",
            "arguments": {"type": "area", "label": "Attic"},
        },
        {
            "tool": "create_location",
            "arguments": {"type": "area", "label": "Garage"},
        },
        {
            "tool": "create_location",
            "arguments": {"type": "cabinet", "label": "Printer Cabinet"},
        },
    ]
    assert client.get("/locations").json()["roots"] == []

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": (
                "Set up my storage. I have an attic, a garage, "
                "and a printer cabinet"
            ),
            "source_interaction_id": body["interaction_id"],
            "plan": body["plan"],
        },
    )

    assert confirmed.status_code == 200
    assert client.get("/locations").json()["roots"] == [
        "attic",
        "garage",
        "printer-cabinet",
    ]


def test_assistant_chat_setup_prompt_preserves_parent_child_storage(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "software-only", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )

    proposed = client.post(
        "/assistant/chat",
        json={
            "text": (
                "Set up my storage. I have a garage with a printer cabinet "
                "and two shelves."
            )
        },
    )

    assert proposed.status_code == 200
    body = proposed.json()
    assert body["message"] == (
        "Create Garage with Printer Cabinet, Shelf 1, and Shelf 2 inside."
    )
    assert body["plan"] == [
        {
            "tool": "create_location",
            "arguments": {"type": "area", "label": "Garage"},
        },
        {
            "tool": "create_location",
            "arguments": {
                "type": "cabinet",
                "label": "Printer Cabinet",
                "parent_id": "Garage",
            },
        },
        {
            "tool": "create_location",
            "arguments": {
                "type": "shelf",
                "label": "Shelf 1",
                "parent_id": "Garage",
            },
        },
        {
            "tool": "create_location",
            "arguments": {
                "type": "shelf",
                "label": "Shelf 2",
                "parent_id": "Garage",
            },
        },
    ]

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "Set up my storage",
            "source_interaction_id": body["interaction_id"],
            "plan": body["plan"],
        },
    )

    assert confirmed.status_code == 200
    locations = {
        location["id"]: location
        for location in client.get("/locations").json()["locations"]
    }
    assert client.get("/locations").json()["roots"] == ["garage"]
    assert locations["printer-cabinet"]["parent_id"] == "garage"
    assert locations["shelf-1"]["parent_id"] == "garage"
    assert locations["shelf-2"]["parent_id"] == "garage"


def test_assistant_chat_setup_mode_plans_nested_storage_without_llm_key(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "software-only", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    assert client.post(
        "/locations",
        json={"id": "garage", "type": "room", "label": "Garage"},
    ).status_code == 200
    clarified = client.post(
        "/assistant/chat",
        json={
            "text": "Help me set up storage inside Garage.",
            "mode": "setup",
            "source": "mobile",
            "current_location_id": "garage",
        },
    ).json()
    assert clarified["mode"] == "clarify"
    assert clarified["decision"]["plan"] == []
    assert "What cabinets" in clarified["message"]
    completed = client.post(
        "/assistant/chat",
        json={
            "text": "Nothing else",
            "mode": "setup",
            "source": "mobile",
            "current_location_id": "garage",
        },
    ).json()
    assert completed["decision"]["action"] == "setup_complete"
    assert completed["message"] == "Garage is ready."
    command = "Printer cabinet with six drawers, a workbench, and two wall shelves"

    proposed = client.post(
        "/assistant/chat",
        json={
            "text": command,
            "mode": "setup",
            "source": "mobile",
            "current_location_id": "garage",
        },
    )

    assert proposed.status_code == 200
    body = proposed.json()
    assert body["action"] == "proposed"
    assert body["message"] == (
        "Set up Garage with Printer Cabinet, 6 Drawers inside Printer Cabinet, "
        "Workbench, 2 Wall Shelves."
    )
    assert body["plan"][0] == {
        "tool": "create_location",
        "arguments": {
            "type": "cabinet",
            "label": "Printer Cabinet",
            "parent_id": "garage",
        },
    }
    assert [step["arguments"]["label"] for step in body["plan"]] == [
        "Printer Cabinet",
        "Drawer 1",
        "Drawer 2",
        "Drawer 3",
        "Drawer 4",
        "Drawer 5",
        "Drawer 6",
        "Workbench",
        "Wall Shelf 1",
        "Wall Shelf 2",
    ]
    assert client.get("/inventory").json()["items"] == []

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": command,
            "source_interaction_id": body["interaction_id"],
            "plan": body["plan"],
        },
    )

    assert confirmed.status_code == 200
    tree = client.get("/locations/garage/tree").json()
    assert {child["id"] for child in tree["children"]} == {
        "printer-cabinet",
        "workbench",
        "wall-shelf-1",
        "wall-shelf-2",
    }
    cabinet = next(child for child in tree["children"] if child["id"] == "printer-cabinet")
    assert {child["id"] for child in cabinet["children"]} == {
        "drawer-1",
        "drawer-2",
        "drawer-3",
        "drawer-4",
        "drawer-5",
        "drawer-6",
    }


def test_assistant_chat_atomically_sets_up_new_root_and_nested_storage(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "software-only", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    command = (
        "Set up Garage as a room. Inside it: "
        "printer cabinet with four drawers and two wall shelves"
    )

    proposed = client.post(
        "/assistant/chat",
        json={"text": command, "mode": "setup_new", "source": "mobile"},
    ).json()

    assert proposed["action"] == "proposed"
    assert proposed["plan"][0] == {
        "tool": "create_location",
        "arguments": {"type": "room", "label": "Garage"},
    }
    assert [step["arguments"]["label"] for step in proposed["plan"]] == [
        "Garage",
        "Printer Cabinet",
        "Drawer 1",
        "Drawer 2",
        "Drawer 3",
        "Drawer 4",
        "Wall Shelf 1",
        "Wall Shelf 2",
    ]
    assert client.get("/locations").json()["roots"] == []

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": command,
            "source_interaction_id": proposed["interaction_id"],
            "plan": proposed["plan"],
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["receipt"] == {
        "schema": "magpie.confirmation_receipt.v1",
        "change_count": 8,
        "focus_location_id": "garage",
        "summary": "Created 8 storage places.",
        "photos": {"uploaded": 0, "attached": 0, "pending": 0},
        "needs_review": False,
        "undo_available": True,
    }
    tree = client.get("/locations/garage/tree").json()
    assert {child["id"] for child in tree["children"]} == {
        "printer-cabinet",
        "wall-shelf-1",
        "wall-shelf-2",
    }

    undone = client.post("/assistant/undo").json()
    assert undone["ok"] is True
    assert client.get("/locations").json()["roots"] == []


def test_confirm_preflight_rejects_full_plan_before_any_storage_write(
    tmp_path,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "software-only", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )

    response = client.post(
        "/assistant/confirm",
        json={
            "command": "Set up the garage and store several USB adapters",
            "plan": [
                {
                    "tool": "create_location",
                    "arguments": {"type": "room", "label": "Garage"},
                },
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "USB-C adapters",
                        "location_id": "Garage",
                        "quantity": "several",
                    },
                },
            ],
        },
    )

    assert response.status_code == 400
    assert "plan step 2" in response.json()["detail"]
    assert client.get("/locations").json()["roots"] == []
    assert client.get("/inventory").json()["items"] == []


def test_assistant_setup_scopes_duplicate_location_names_and_undoes_new_tree(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "software-only", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    for location in (
        {"id": "basement", "type": "room", "label": "Basement"},
        {"id": "garage", "type": "room", "label": "Garage"},
        {
            "id": "printer-cabinet",
            "type": "cabinet",
            "label": "Printer Cabinet",
            "parent_id": "basement",
            "purpose_tags": ["resin"],
        },
    ):
        assert client.post("/locations", json=location).status_code == 200

    proposed = client.post(
        "/assistant/chat",
        json={
            "text": "Printer cabinet with two drawers",
            "mode": "setup",
            "source": "mobile",
            "current_location_id": "garage",
        },
    ).json()
    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "Printer cabinet with two drawers",
            "source_interaction_id": proposed["interaction_id"],
            "plan": proposed["plan"],
        },
    )

    assert confirmed.status_code == 200
    basement = client.get("/locations/basement/tree").json()
    garage = client.get("/locations/garage/tree").json()
    assert [child["id"] for child in basement["children"]] == ["printer-cabinet"]
    assert client.get("/locations/printer-cabinet").json()["location"]["purpose_tags"] == [
        "resin"
    ]
    assert [child["id"] for child in garage["children"]] == ["printer-cabinet-2"]
    assert {child["id"] for child in garage["children"][0]["children"]} == {
        "drawer-1",
        "drawer-2",
    }

    undone = client.post("/assistant/undo").json()
    assert undone["ok"] is True
    assert client.get("/locations/garage/tree").json()["children"] == []
    assert [child["id"] for child in client.get("/locations/basement/tree").json()["children"]] == [
        "printer-cabinet"
    ]


def test_assistant_chat_manages_location_lifecycle_without_llm_key(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "software-only", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    client.post("/locations", json={"label": "Garage", "type": "area"})
    client.post(
        "/locations",
        json={"label": "Printer Cabinet", "type": "cabinet"},
    )
    client.post(
        "/locations",
        json={
            "label": "Parts Shelf",
            "type": "shelf",
            "parent_id": "printer-cabinet",
        },
    )

    for spoken_request in (
        {"text": "Create a shed", "source": "voice"},
        {"text": "Create a Hall Closet", "source": "voice"},
        {"text": "Move Printer Cabinet into Garage", "source": "voice"},
        {
            "text": "Garage with two shelves",
            "mode": "setup_new",
            "source": "voice",
        },
    ):
        blocked = client.post("/assistant/chat", json=spoken_request).json()
        assert blocked["decision"]["action"] == "place_configuration_ui_required", (
            spoken_request,
            blocked,
        )
        assert blocked["source"] == "voice"
        assert blocked["decision"]["places_url"] == "/storage?view=places"
        assert blocked["decision"]["plan"] == []
        assert "plan" not in blocked
    assert client.get("/locations/hall-closet").status_code == 404
    assert client.get("/locations/printer-cabinet").json()["location"]["parent_id"] is None

    moved = client.post(
        "/assistant/chat",
        json={"text": "Move Printer Cabinet into Garage"},
    ).json()
    assert moved["action"] == "proposed"
    assert moved["plan"] == [
        {
            "tool": "move_container",
            "arguments": {
                "location_id": "printer-cabinet",
                "new_parent_id": "garage",
            },
        }
    ]
    confirmed_move = client.post(
        "/assistant/confirm",
        json={
            "command": "Move Printer Cabinet into Garage",
            "source_interaction_id": moved["interaction_id"],
            "plan": moved["plan"],
        },
    )
    assert confirmed_move.status_code == 200
    assert client.get("/locations/printer-cabinet").json()["location"]["parent_id"] == "garage"

    cycle = client.post(
        "/assistant/chat",
        json={"text": "Move Garage into Parts Shelf"},
    ).json()
    assert cycle["mode"] == "clarify"
    assert "cannot be moved inside itself" in cycle["message"]

    undone = client.post("/assistant/undo").json()
    assert undone["ok"] is True
    assert client.get("/locations/printer-cabinet").json()["location"]["parent_id"] is None

    moved_again = client.post(
        "/assistant/chat",
        json={"text": "Put Printer Cabinet in Garage"},
    ).json()
    client.post(
        "/assistant/confirm",
        json={
            "command": "Put Printer Cabinet in Garage",
            "source_interaction_id": moved_again["interaction_id"],
            "plan": moved_again["plan"],
        },
    )

    blocked_archive = client.post(
        "/assistant/chat",
        json={
            "text": "Archive Garage",
            "source": "voice",
            "voice_transcript": "Magpie archive garage",
            "voice_command": "archive garage",
        },
    ).json()
    assert blocked_archive["source"] == "voice"
    assert blocked_archive["voice"]["command"] == "archive garage"
    assert (
        blocked_archive["decision"]["action"]
        == "place_configuration_ui_required"
    )
    assert blocked_archive["decision"]["places_url"] == "/storage?view=places"
    assert blocked_archive["decision"]["plan"] == []
    assert "plan" not in blocked_archive

    archived = client.post(
        "/assistant/chat",
        json={"text": "Archive Garage"},
    ).json()
    assert archived["plan"] == [
        {
            "tool": "archive_location",
            "arguments": {"location_id": "garage"},
        }
    ]
    confirmed_archive = client.post(
        "/assistant/confirm",
        json={
            "command": "Archive Garage",
            "source_interaction_id": archived["interaction_id"],
            "plan": archived["plan"],
        },
    ).json()
    assert set(confirmed_archive["applied"][0]["archived_ids"]) == {
        "garage",
        "printer-cabinet",
        "parts-shelf",
    }
    assert client.get("/locations").json()["roots"] == []

    restored = client.post(
        "/assistant/chat",
        json={"text": "Restore Garage"},
    ).json()
    assert restored["plan"] == [
        {
            "tool": "restore_location",
            "arguments": {"location_id": "garage"},
        }
    ]
    client.post(
        "/assistant/confirm",
        json={
            "command": "Restore Garage",
            "source_interaction_id": restored["interaction_id"],
            "plan": restored["plan"],
        },
    )
    assert client.get("/locations").json()["roots"] == ["garage"]
    assert client.get("/locations/printer-cabinet").json()["location"]["parent_id"] == "garage"

    missing = client.post(
        "/assistant/chat",
        json={"text": "Archive Missing Shed"},
    ).json()
    assert missing["mode"] == "clarify"
    assert "could not find" in missing["message"]
    assert client.get("/inventory").json()["items"] == []


def test_assistant_chat_keeps_voice_scoped_to_retrieval_and_putaway(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="wled",
                wled_url="http://127.0.0.1:1",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    client.post("/inventory", json={"name": "M4 T-nuts", "bin_id": "B001"})

    for command in (
        "move M4 T-nuts to another bin",
        "learn that hardware belongs in the cabinet",
        "print labels for the cabinet",
        "list my storage places",
        "how many items do I have",
        "use a larger bin",
    ):
        blocked = client.post(
            "/assistant/chat",
            json={"text": command, "mode": "agent", "source": "voice"},
        ).json()
        assert blocked["mode"] == "clarify"
        assert blocked["decision"]["action"] == "voice_scope_ui_required"
        assert blocked["decision"]["assistant_url"] == "/"
        assert blocked["decision"]["storage_url"] == "/storage?view=places"
        assert blocked["message"] == VOICE_SCOPE_MESSAGE
        assert blocked["decision"]["plan"] == []
        assert blocked["source"] == "voice"

    latest_scope = client.get(
        "/assistant/interactions?limit=1"
    ).json()["interactions"][0]
    assert latest_scope["route"] == "/assistant/chat"
    assert latest_scope["command"] == "use a larger bin"
    assert (
        latest_scope["response"]["decision"]["action"]
        == "voice_scope_ui_required"
    )
    kiosk_scope = client.get(
        "/kiosk/state",
        params={"after_id": latest_scope["id"] - 1, "wait_seconds": 0},
    ).json()["interactions"]
    assert [row["id"] for row in kiosk_scope] == [latest_scope["id"]]

    found = client.post(
        "/assistant/chat",
        json={"text": "find M4 T-nuts", "source": "voice"},
    ).json()
    put_away = client.post(
        "/assistant/chat",
        json={"text": "put away M4 T-nuts", "source": "voice"},
    ).json()

    assert found["mode"] == "find"
    assert found["results"][0]["name"] == "M4 T-nuts"
    assert put_away["mode"] == "ask"
    assert put_away["decision"]["action"] == "put_away"


def test_voice_putaway_new_container_resumes_after_ui_destination_choice(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=2,
                backend="wled",
                wled_url="http://127.0.0.1:1",
                layout_path=str(layout_path),
                database_path=str(database_path),
            )
        )
    )
    photo = client.post(
        "/photos",
        json={
            "filename": "m3-screws.jpg",
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(b"m3-screws").decode("ascii"),
        },
    ).json()["photo"]
    locations_before = client.get("/locations").json()["locations"]

    blocked = client.post(
        "/assistant/chat",
        json={
            "text": (
                "Magpie, put these M3 flush mount screws in their own "
                "small bin in B002"
            ),
            "source": "voice",
            "photo_ids": [photo["id"]],
        },
    ).json()

    assert blocked["decision"]["action"] == "place_configuration_ui_required"
    assert blocked["decision"]["plan"] == []
    assert blocked["decision"]["places_url"] == (
        f"/storage?view=places&resume={blocked['interaction_id']}&location=B002"
    )
    assert client.get("/locations").json()["locations"] == locations_before

    handoff = client.get(
        f"/assistant/handoffs/{blocked['interaction_id']}"
    ).json()
    assert handoff == {
        "ok": True,
        "interaction_id": blocked["interaction_id"],
        "kind": "put_away_destination",
        "command": (
            "put these M3 flush mount screws in their own small bin in B002"
        ),
        "resume_command": "Put away M3 flush mount screws",
        "photo_ids": [photo["id"]],
        "current_location_id": None,
        "suggested_container": {
            "label": "small bin",
            "type": "bin",
            "capacity_class": "small",
            "parent_hint": "B002",
            "parent_id": "B002",
        },
    }

    resumed = client.post(
        "/assistant/chat",
        json={
            "text": handoff["resume_command"],
            "source": "mobile",
            "photo_ids": handoff["photo_ids"],
            "current_location_id": "B002",
        },
    ).json()
    plan = resumed["decision"]["plan"]

    assert plan[0]["tool"] == "put_item"
    assert plan[0]["arguments"]["item"] == "M3 Flush Mount Screws"
    assert plan[0]["arguments"]["location_id"] == "B002"
    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": handoff["resume_command"],
            "photo_ids": handoff["photo_ids"],
            "source_interaction_id": resumed["interaction_id"],
            "plan": plan,
        },
    ).json()
    item_id = confirmed["applied"][0]["item_id"]
    inventory = client.get("/inventory").json()["items"]
    stored = next(item for item in inventory if item["item_id"] == item_id)

    assert stored["bin_id"] == "B002"
    assert stored["photos"] == [f"/photos/{photo['id']}"]


def test_voice_putaway_handoff_resolves_spoken_parent_articles(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {"name": "software-only", "columns": 1, "rows_bottom_to_top": []}
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    assert client.post(
        "/locations",
        json={"id": "attic", "label": "Attic", "type": "room"},
    ).status_code == 200

    blocked = client.post(
        "/assistant/chat",
        json={
            "text": "put away M5 screws in their own new small bin in the attic",
            "source": "voice",
        },
    ).json()

    assert blocked["decision"]["action"] == "place_configuration_ui_required"
    assert blocked["decision"]["places_url"] == (
        f"/storage?view=places&resume={blocked['interaction_id']}&location=attic"
    )
    handoff = client.get(
        f"/assistant/handoffs/{blocked['interaction_id']}"
    ).json()
    assert handoff["resume_command"] == "Put away M5 screws"
    assert handoff["suggested_container"] == {
        "label": "new small bin",
        "type": "bin",
        "capacity_class": "small",
        "parent_hint": "the attic",
        "parent_id": "attic",
    }


def test_first_putaway_hands_empty_workspace_to_visual_storage_setup(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {"name": "software-only", "columns": 1, "rows_bottom_to_top": []}
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    photo = client.post(
        "/photos",
        json={
            "filename": "zip-ties.jpg",
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(b"zip-ties").decode("ascii"),
        },
    ).json()["photo"]

    blocked = client.post(
        "/assistant/chat",
        json={
            "text": "Put away zip ties",
            "source": "ui",
            "photo_ids": [photo["id"]],
        },
    ).json()

    decision = blocked["decision"]
    assert decision["action"] == "place_configuration_ui_required"
    assert decision["plan"] == []
    assert decision["places_url"] == (
        f"/storage?view=places&resume={blocked['interaction_id']}"
    )
    assert "kept Zip Ties and any photos ready" in decision["message"]
    assert client.get("/locations").json()["roots"] == []

    handoff = client.get(
        f"/assistant/handoffs/{blocked['interaction_id']}"
    ).json()
    assert handoff == {
        "ok": True,
        "interaction_id": blocked["interaction_id"],
        "kind": "put_away_destination",
        "command": "zip ties",
        "resume_command": "Put away Zip Ties",
        "photo_ids": [photo["id"]],
        "current_location_id": None,
        "suggested_container": {
            "label": "General storage",
            "type": "bin",
            "capacity_class": "small",
            "parent_hint": "",
            "parent_id": None,
        },
    }


def test_putaway_without_item_destination_suggests_visual_child_setup(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {"name": "software-only", "columns": 1, "rows_bottom_to_top": []}
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    workshop = client.post(
        "/locations",
        json={"label": "Workshop", "type": "area", "accepts_items": False},
    ).json()["id"]

    blocked = client.post(
        "/assistant/chat",
        json={"text": "Put away cordless circular saw", "source": "mobile"},
    ).json()
    handoff = client.get(
        f"/assistant/handoffs/{blocked['interaction_id']}"
    ).json()

    assert blocked["decision"]["action"] == "place_configuration_ui_required"
    assert handoff["resume_command"] == "Put away Cordless Circular Saw"
    assert handoff["suggested_container"] == {
        "label": "Cordless Circular Saw tote",
        "type": "tote",
        "capacity_class": "large",
        "parent_hint": "",
        "parent_id": None,
    }
    assert client.get("/locations").json()["roots"] == [workshop]

    destination = client.post(
        "/locations",
        json={
            "label": "Cordless Circular Saw tote",
            "type": "tote",
            "parent_id": workshop,
            "capacity_class": "large",
            "accepts_items": True,
        },
    ).json()["location"]
    resumed = client.post(
        "/assistant/chat",
        json={
            "text": handoff["resume_command"],
            "source": "mobile",
            "current_location_id": destination["id"],
        },
    ).json()
    plan = resumed["decision"]["plan"]

    assert plan[0]["arguments"]["size_class"] == "large"
    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": handoff["resume_command"],
            "plan": plan,
            "source_interaction_id": resumed["interaction_id"],
        },
    )

    assert confirmed.status_code == 200
    stored = client.get("/inventory").json()["items"]
    assert stored[0]["name"] == "Cordless Circular Saw"
    assert stored[0]["size_class"] == "large"


def test_assistant_confirmation_replays_without_applying_additive_plan_twice(
    tmp_path,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "software-only", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    repositories = StorageRepositories.sqlite(tmp_path / "inventory.db")
    repositories.locations.upsert(Location(id="garage", type="area", label="Garage"))
    plan = [
        {
            "tool": "put_item",
            "arguments": {
                "item": "Washers",
                "location_id": "garage",
                "quantity": 1,
                "unit": "each",
                "quantity_mode": "add",
            },
        }
    ]
    source_interaction_id = repositories.inventory.record_assistant_interaction(
        route="/assistant/chat",
        command="Add one washer",
        response={"plan": plan},
        status="pending",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            ),
            repositories=repositories,
        )
    )
    payload = {
        "command": "Add one washer",
        "plan": plan,
        "source_interaction_id": source_interaction_id,
    }

    first = client.post("/assistant/confirm", json=payload)
    replay = client.post("/assistant/confirm", json=payload)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json()["confirmation_interaction_id"] > source_interaction_id
    assert replay.json() == {**first.json(), "idempotent_replay": True}
    [stored] = client.get("/inventory").json()["items"]
    assert stored["quantity"] == 1
    source = repositories.inventory.assistant_interaction(source_interaction_id)
    assert source["status"] == "confirmed"
    assert source["response"]["confirmed_plan"] == plan
    assert source["response"]["confirmation_response"] == first.json()
    confirms = [
        interaction
        for interaction in repositories.inventory.assistant_interactions_after(limit=20)
        if interaction["route"] == "/assistant/confirm"
    ]
    assert len(confirms) == 1
    assert confirms[0]["id"] == first.json()["confirmation_interaction_id"]


def test_assistant_chat_clarifies_duplicate_location_labels_before_archiving(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "software-only", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(database_path),
            )
        )
    )
    location_store = LocationStore(database_path)
    location_store.upsert(Location(id="garage-parts", label="Parts", type="box"))
    _insert_legacy_location(
        database_path,
        location_id="attic-parts",
        location_type="box",
        label="Parts",
    )

    response = client.post(
        "/assistant/chat",
        json={"text": "Archive Parts"},
    ).json()

    assert response["mode"] == "clarify"
    assert response["decision"]["action"] == "clarify"
    assert {row["id"] for row in response["decision"]["location_candidates"]} == {
        "garage-parts",
        "attic-parts",
    }
    assert client.get("/locations").json()["roots"] == ["attic-parts", "garage-parts"]


def test_assistant_chat_resolves_duplicate_location_by_qualified_path(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "software-only", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    for location in (
        {"id": "basement", "label": "Basement", "type": "room"},
        {"id": "garage", "label": "Garage", "type": "room"},
        {
            "id": "printer-cabinet",
            "label": "Printer Cabinet",
            "type": "cabinet",
            "parent_id": "basement",
        },
        {
            "id": "printer-cabinet-2",
            "label": "Printer Cabinet",
            "type": "cabinet",
            "parent_id": "garage",
        },
    ):
        assert client.post("/locations", json=location).status_code == 200

    ambiguous = client.post(
        "/assistant/chat", json={"text": "Archive Printer Cabinet"}
    ).json()
    assert ambiguous["decision"]["action"] == "clarify"
    assert "Basement > Printer Cabinet" in ambiguous["message"]
    assert "Garage > Printer Cabinet" in ambiguous["message"]
    assert {
        tuple(node["label"] for node in candidate["path"])
        for candidate in ambiguous["decision"]["location_candidates"]
    } == {
        ("Basement", "Printer Cabinet"),
        ("Garage", "Printer Cabinet"),
    }
    assert {
        candidate["selection_command"]
        for candidate in ambiguous["decision"]["location_candidates"]
    } == {
        "Archive Basement > Printer Cabinet",
        "Archive Garage > Printer Cabinet",
    }

    qualified = client.post(
        "/assistant/chat", json={"text": "Archive Garage > Printer Cabinet"}
    ).json()
    assert qualified["action"] == "proposed"
    assert qualified["plan"] == [
        {
            "tool": "archive_location",
            "arguments": {"location_id": "printer-cabinet-2"},
        }
    ]


def test_assistant_chat_can_create_first_storage_tree_without_llm_key(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "software-only", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=0,
        backend="none",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    proposed = client.post(
        "/assistant/chat",
        json={
            "text": "Create an Attic storage space with a Roaster Parts Bin inside",
            "mode": "auto",
        },
    )

    assert proposed.status_code == 200
    body = proposed.json()
    assert body["mode"] == "agent"
    assert body["action"] == "proposed"
    assert body["plan"] == [
        {
            "tool": "create_location",
            "arguments": {"type": "area", "label": "Attic"},
        },
        {
            "tool": "create_location",
            "arguments": {
                "type": "bin",
                "label": "Roaster Parts Bin",
                "parent_id": "Attic",
            },
        },
    ]
    pending = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert pending["status"] == "pending"

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "Create an Attic storage space with a Roaster Parts Bin inside",
            "source_interaction_id": body["interaction_id"],
            "plan": body["plan"],
        },
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["applied"] == [
        {"created_location": "attic"},
        {"created_location": "roaster-parts-bin"},
    ]
    roots = client.get("/locations").json()["roots"]
    assert roots == ["attic"]
    child = client.get("/locations/roaster-parts-bin").json()["location"]
    assert child["parent_id"] == "attic"

    put_away = client.post(
        "/assistant/chat",
        json={
            "text": "Put away a spare roaster thermocouple in Roaster Parts Bin",
            "mode": "auto",
        },
    ).json()
    decision = put_away["decision"]
    assert decision["item"]["name"] == "Spare Roaster Thermocouple"
    assert decision["destination"]["id"] == "roaster-parts-bin"

    stored = client.post(
        "/assistant/confirm",
        json={
            "command": "Put away a spare roaster thermocouple in Roaster Parts Bin",
            "source_interaction_id": put_away["interaction_id"],
            "plan": decision["plan"],
        },
    )
    assert stored.status_code == 200
    assert stored.json()["applied"][0]["location_id"] == "roaster-parts-bin"

    found = client.post(
        "/assistant/chat",
        json={"text": "Find spare roaster thermocouple", "mode": "auto"},
    ).json()
    assert found["mode"] == "find"
    assert found["results"][0]["name"] == "Spare Roaster Thermocouple"
    assert found["results"][0]["location_id"] == "roaster-parts-bin"


def test_find_session_accumulates_colors_until_clear(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "searchable_storage_led.controller.WledWriter.write_pixels",
        lambda self, pixels, config: None,
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post("/inventory", json={"name": "USB drive", "bin_id": "B001"})
    client.post("/inventory", json={"name": "USB dongle", "bin_id": "B002"})

    first = client.post("/assistant/find", json={"query": "USB drive"}).json()
    second = client.post("/assistant/find", json={"query": "USB dongle"}).json()
    active = client.get("/lights/active").json()["active_finds"]

    assert first["light_assignments"][0]["color"] == [255, 0, 0]
    assert second["light_assignments"][0]["color"] == [0, 70, 255]
    assert [entry["query"] for entry in active] == ["USB drive", "USB dongle"]
    assert [entry["kind"] for entry in active] == ["find", "find"]
    assert active[0]["matches"][0]["lit_location"] == "B001"
    assert active[1]["matches"][0]["lit_location"] == "B002"
    assert client.get("/voice/status").json()["voice"]["active_finds"] == active

    cleared = client.post("/clear").json()

    assert cleared["active_finds"] == []
    assert client.get("/lights/active").json()["active_finds"] == []


def test_workspace_find_only_reports_lit_for_a_command_ready_cabinet(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 0, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    locations = LocationStore(database_path)
    locations.upsert(
        Location(
            id="main-cabinet",
            type="cabinet",
            label="Main Cabinet",
            light_target="main-cabinet",
            accepts_items=False,
        )
    )
    locations.upsert(
        Location(
            id="B001",
            type="bin",
            parent_id="main-cabinet",
            label="B001",
            light_target="main-cabinet",
            light_indexes=[0],
        )
    )
    inventory = InventoryStore(database_path)
    item_id = inventory.upsert_item(name="USB drive")
    inventory.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")
    edge = SqliteEdgeRepository(
        tmp_path / "edge.db",
        workspace_id="workspace-1",
    )
    edge_ready = False
    original_edge_status = edge.status

    def edge_status():
        status = original_edge_status()
        if not edge_ready:
            return {**status, "devices": []}
        return {
            **status,
            "devices": [
                {
                    "device_id": "main-cabinet",
                    "enrolled": True,
                    "last_seen_at": time.time(),
                    "credential_scopes": ["commands"],
                    "capabilities": ["set_pixels"],
                }
            ],
        }

    edge.status = edge_status  # type: ignore[method-assign]
    cfg = LedConfig(
        pixel_count=0,
        backend="none",
        layout_path=str(layout_path),
        database_path=str(database_path),
        storage=StorageConfig(
            backend="supabase",
            supabase_url="https://project.supabase.co",
            workspace_id="",
        ),
        auth=AuthConfig(
            mode="supabase",
            supabase_url="https://project.supabase.co",
            multi_workspace=True,
        ),
        edge=EdgeConfig(mode="workspace"),
    )
    repositories = StorageRepositories.sqlite(database_path)
    client = TestClient(
        create_app(
            cfg,
            repositories=StorageRepositories(
                inventory=repositories.inventory,
                locations=repositories.locations,
                backend="supabase",
                workspace_id="workspace-1",
            ),
            edge_repository=edge,
            auth_service=SimpleNamespace(
                authenticate=lambda *args, **kwargs: AuthIdentity(
                    "test-user",
                    "test@example.com",
                    "workspace-1",
                    "owner",
                ),
                close=lambda: None,
            ),
            workspace_member_router=SimpleNamespace(close=lambda: None),
            workspace_catalog_service=object(),
            agent_credential_service=object(),
        )
    )
    headers = {"Authorization": "Bearer test-token"}

    offline = client.post(
        "/assistant/find",
        json={"query": "USB drive"},
        headers=headers,
    ).json()

    assert offline["results"][0]["location_id"] == "B001"
    assert offline["lit"] is False
    assert edge.status()["recent_commands"] == []

    edge_ready = True

    online = client.post(
        "/assistant/find",
        json={"query": "USB drive"},
        headers=headers,
    ).json()

    assert online["lit"] is True
    command = edge.claim("main-cabinet", claim_timeout=5, max_attempts=3)
    assert command is not None
    assert command.kind == "set_pixels"
    assert command.payload["pixels"] == [[102, 0, 0]]


def test_active_find_session_survives_restart_until_clear(
    tmp_path,
    monkeypatch,
) -> None:
    writes = []

    def capture_write(self, pixels, config) -> None:
        writes.append(list(pixels))

    monkeypatch.setattr(
        "searchable_storage_led.controller.WledWriter.write_pixels",
        capture_write,
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    first_client = TestClient(create_app(cfg))
    first_client.post("/inventory", json={"name": "USB drive", "bin_id": "B001"})
    first_client.post("/inventory", json={"name": "USB dongle", "bin_id": "B002"})
    first_client.post("/assistant/find", json={"query": "USB drive"})
    before_restart = first_client.post(
        "/assistant/find", json={"query": "USB dongle"}
    ).json()["active_finds"]

    writes.clear()
    restarted_client = TestClient(create_app(cfg))
    restored = restarted_client.get("/lights/active").json()["active_finds"]

    assert [entry["query"] for entry in restored] == ["USB drive", "USB dongle"]
    assert [entry["color"] for entry in restored] == [
        entry["color"] for entry in before_restart
    ]
    assert writes

    restarted_client.post("/clear")
    after_clear_restart = TestClient(create_app(cfg))
    assert after_clear_restart.get("/lights/active").json()["active_finds"] == []


def test_lit_find_reconciles_when_its_location_moves_and_archives(
    tmp_path,
    monkeypatch,
) -> None:
    writes = []

    def capture_write(self, pixels, config) -> None:
        writes.append(list(pixels))

    monkeypatch.setattr(
        "searchable_storage_led.controller.WledWriter.write_pixels",
        capture_write,
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="wled",
                wled_url="http://127.0.0.1:1",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    workshop = client.post(
        "/locations",
        json={"label": "Workshop", "type": "room"},
    ).json()["id"]
    client.post("/inventory", json={"name": "USB drive", "bin_id": "B001"})
    found = client.post("/assistant/find", json={"query": "USB drive"}).json()

    assert found["active_finds"][0]["matches"][0]["lit_location"] == "B001"
    writes.clear()
    moved = client.post(
        "/locations/B001/move",
        json={"new_parent_id": workshop},
    ).json()

    assert moved["light_ok"] is True
    assert moved["light_errors"] == []
    assert [
        node["label"]
        for node in moved["active_finds"][0]["matches"][0]["path"]
    ] == ["Workshop", "B001"]
    assert len(writes) >= 2

    writes.clear()
    archived = client.post(f"/locations/{workshop}/archive").json()

    assert archived["active_finds"] == []
    assert archived["light_ok"] is True
    assert archived["light_errors"] == []
    assert writes


def test_clear_removes_active_find_state_when_light_controller_is_offline(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "searchable_storage_led.controller.WledWriter.write_pixels",
        lambda self, pixels, config: None,
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="wled",
                wled_url="http://127.0.0.1:1",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    client.post("/inventory", json={"name": "USB drive", "bin_id": "B001"})
    assert client.post("/assistant/find", json={"query": "USB drive"}).json()[
        "active_finds"
    ]

    def unavailable_writer(self, pixels, config) -> None:
        raise RuntimeError("controller offline")

    monkeypatch.setattr(
        "searchable_storage_led.controller.WledWriter.write_pixels",
        unavailable_writer,
    )
    response = client.post("/clear")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["active_finds"] == []
    assert response.json()["light_ok"] is False
    assert response.json()["light_errors"] == ["controller offline"]
    assert "did not confirm" in response.json()["message"]
    assert client.get("/lights/active").json()["active_finds"] == []


def test_putaway_joins_find_session_and_confirmation_restores_colors(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "searchable_storage_led.controller.WledWriter.write_pixels",
        lambda self, pixels, config: None,
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    results = iter(
        [
            AgentResult(
                message="USB drive is in B001.",
                trace=[
                    {
                        "tool": "search_items",
                        "arguments": {"query": "USB drive"},
                        "result": [
                            {
                                "name": "USB drive",
                                "item_id": 1,
                                "location_id": "B001",
                                "nearest_lit_location_id": "B001",
                                "path": [{"id": "B001", "label": "B001"}],
                            }
                        ],
                    }
                ],
            ),
            AgentResult(
                message="Put M3 screws in B002.",
                plan=[
                    {
                        "tool": "put_item",
                        "arguments": {
                            "item": "M3 screws",
                            "location_id": "B002",
                            "quantity": 12,
                        },
                    }
                ],
            ),
        ]
    )
    monkeypatch.setattr(
        "searchable_storage_led.api.run_agent",
        lambda *args, **kwargs: next(results),
    )
    client = TestClient(create_app(cfg))

    found = client.post(
        "/assistant/chat",
        json={"text": "find USB drive", "mode": "agent"},
    ).json()
    proposed = client.post(
        "/assistant/chat",
        json={"text": "put away these M3 screws", "mode": "agent"},
    ).json()
    active = client.get("/lights/active").json()["active_finds"]
    state_before_confirm = client.get("/state").json()["pixels"]

    assert found["light_assignments"][0]["color"] == [255, 0, 0]
    assert proposed["light_assignments"][0]["color"] == [0, 70, 255]
    assert proposed["confirmation"]["rows"][0]["color"] == {
        "name": "Blue",
        "rgb": [0, 70, 255],
    }
    assert proposed["confirmation"]["rows"][0]["light"] == {
        "location_id": "B002",
        "status": "lit",
    }
    assert [entry["kind"] for entry in active] == ["find", "put_away"]
    assert [entry["matches"][0]["name"] for entry in active] == [
        "USB drive",
        "M3 screws",
    ]
    assert state_before_confirm[0] != [0, 0, 0]
    assert state_before_confirm[1] != [0, 0, 0]

    confirmed = client.post("/assistant/voice/confirm").json()

    confirmed_active = confirmed["active_finds"]
    assert confirmed["active_find_removed"] is True
    assert confirmed["light_effect"] == "save-pulse"
    assert confirmed["light_seconds"] == 2.0
    assert confirmed_active == [active[0]]
    state_after_confirm = client.get("/state").json()["pixels"]
    assert state_after_confirm[0] == [0, 0, 0]
    assert state_after_confirm[1] != [0, 0, 0]
    assert state_after_confirm != state_before_confirm
    assert client.get("/lights/active").json()["active_finds"] == confirmed_active


def test_putaway_shares_existing_location_color_without_replacing_find(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "searchable_storage_led.controller.WledWriter.write_pixels",
        lambda self, pixels, config: None,
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    results = iter(
        [
            AgentResult(
                message="M4 T-nuts are in B001.",
                trace=[
                    {
                        "tool": "search_items",
                        "arguments": {"query": "M4 T-nuts"},
                        "result": [
                            {
                                "name": "M4 T-nuts",
                                "item_id": 1,
                                "location_id": "B001",
                                "nearest_lit_location_id": "B001",
                                "path": [{"id": "B001", "label": "B001"}],
                            }
                        ],
                    }
                ],
            ),
            AgentResult(
                message="Put M3 washers in B001.",
                plan=[
                    {
                        "tool": "put_item",
                        "arguments": {
                            "item": "M3 washers",
                            "location_id": "B001",
                            "quantity": 1,
                        },
                    }
                ],
            ),
        ]
    )
    monkeypatch.setattr(
        "searchable_storage_led.api.run_agent",
        lambda *args, **kwargs: next(results),
    )
    client = TestClient(create_app(cfg))
    client.post("/inventory", json={"name": "M4 T-nuts", "bin_id": "B001"})

    found = client.post(
        "/assistant/chat",
        json={"text": "find M4 T-nuts", "mode": "agent"},
    ).json()
    proposed = client.post(
        "/assistant/chat",
        json={"text": "put away M3 washers", "mode": "agent"},
    ).json()
    active = client.get("/lights/active").json()["active_finds"]

    assert [entry["kind"] for entry in active] == ["find", "put_away"]
    assert [entry["matches"][0]["name"] for entry in active] == [
        "M4 T-nuts",
        "M3 washers",
    ]
    assert {entry["matches"][0]["lit_location"] for entry in active} == {"B001"}
    assert {tuple(entry["color"]) for entry in active} == {(255, 0, 0)}
    assert proposed["confirmation"]["rows"][0]["color"] == {
        "name": "Red",
        "rgb": [255, 0, 0],
    }

    canceled = client.post(
        "/assistant/cancel",
        json={
            "command": "Put away M3 washers",
            "source_interaction_id": proposed["interaction_id"],
        },
    ).json()

    assert canceled["active_find_removed"] is True
    assert canceled["active_finds"] == found["active_finds"]
    assert (
        client.get("/lights/active").json()["active_finds"]
        == found["active_finds"]
    )


def test_fast_putaway_joins_existing_color_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "searchable_storage_led.controller.WledWriter.write_pixels",
        lambda self, pixels, config: None,
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post("/inventory", json={"name": "USB drive", "bin_id": "B001"})

    found = client.post("/assistant/chat", json={"text": "find USB drive"}).json()
    proposed = client.post(
        "/assistant/chat",
        json={"text": "put away M3 screws"},
    ).json()
    decision = proposed["decision"]
    active = client.get("/lights/active").json()["active_finds"]

    assert found["raw"]["light_assignments"][0]["color"] == [255, 0, 0]
    assert decision["destination"]["id"] == "B002"
    assert decision["light_assignments"][0]["color"] == [0, 70, 255]
    assert decision["confirmation"]["rows"][0]["color"] == {
        "name": "Blue",
        "rgb": [0, 70, 255],
    }
    assert decision["confirmation"]["rows"][0]["light"] == {
        "location_id": "B002",
        "status": "lit",
    }
    assert [entry["kind"] for entry in active] == ["find", "put_away"]
    assert [entry["matches"][0]["lit_location"] for entry in active] == [
        "B001",
        "B002",
    ]


def test_bare_voice_clear_ends_active_find_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "searchable_storage_led.controller.WledWriter.write_pixels",
        lambda self, pixels, config: None,
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post("/inventory", json={"name": "USB drive", "bin_id": "B001"})
    assert client.post("/assistant/find", json={"query": "USB drive"}).json()[
        "active_finds"
    ]

    response = client.post(
        "/assistant/chat",
        json={"text": "clear", "source": "voice"},
    ).json()

    assert response["decision"]["action"] == "lights_cleared"
    assert response["decision"]["active_finds"] == []
    assert response["source"] == "voice"
    assert client.get("/lights/active").json()["active_finds"] == []


def test_assistant_chat_find_relaxes_spoken_container_framing(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post("/inventory", json={"name": "M3 machine screws", "bin_id": "B001"})

    response = client.post(
        "/assistant/chat",
        json={
            "text": "where is the box of M3 screws",
            "source": "voice",
            "voice_transcript": "Magpie, where is the box of M3 screws?",
        },
    ).json()

    assert response["mode"] == "find"
    assert response["results"][0]["name"] == "M3 machine screws"
    assert response["results"][0]["location_id"] == "B001"


def test_assistant_chat_voice_source_is_visible_on_interaction(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post("/inventory", json={"name": "M4 T-nuts", "bin_id": "B001"})

    response = client.post(
        "/assistant/chat",
        json={
            "text": "find M4 T-nuts",
            "source": "voice",
            "voice_transcript": "Magpie find m four tea nuts",
            "voice_command": "find m four tea nuts",
            "voice_query": "find M4 T-nuts",
            "stt_model": "gpt-4o-mini-transcribe",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "voice"
    assert body["voice"]["transcript"] == "Magpie find m four tea nuts"
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert latest["route"] == "/assistant/chat"
    assert latest["response"]["source"] == "voice"
    assert latest["response"]["voice"] == {
        "transcript": "Magpie find m four tea nuts",
        "command": "find m four tea nuts",
        "query": "find M4 T-nuts",
        "stt_model": "gpt-4o-mini-transcribe",
    }


def test_assistant_chat_lights_explicit_spoken_location_without_agent(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "searchable_storage_led.controller.WledWriter.write_pixels",
        lambda self, pixels, config: None,
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [{"size": "small", "bin_count": 1, "spans": [1]}],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/chat",
        json={
            "text": "can you light B1",
            "source": "voice",
            "voice_transcript": "Hey Magpie, can you light B1?",
        },
    ).json()

    assert response["action"] == "lit_location"
    assert response["lit"] is True
    assert response["message"] == "Lit B001."
    assert response["light_assignments"] == [
        {
            "location_id": "B001",
            "lit_location": "B001",
            "color": [255, 0, 0],
        }
    ]
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert latest["response"]["action"] == "lit_location"
    assert latest["response"]["source"] == "voice"


def test_assistant_agent_surfaces_hardware_tool_result(tmp_path, monkeypatch) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [{"size": "small", "bin_count": 1, "spans": [1]}],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    monkeypatch.setattr(
        "searchable_storage_led.api.run_agent",
        lambda *args, **kwargs: AgentResult(
            message="B001 is lit blue.",
            trace=[
                {
                    "tool": "light_location",
                    "arguments": {"location_id": "B001", "color": "blue"},
                    "result": {
                        "ok": True,
                        "lit": True,
                        "location_id": "B001",
                        "lit_location": "B001",
                        "color": (40, 120, 255),
                    },
                }
            ],
        ),
    )
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/chat",
        json={"text": "show me B001 in blue", "mode": "agent"},
    ).json()

    assert response["action"] == "lit_location"
    assert response["message"] == "Lit B001."
    assert response["lit"] is True
    assert response["light_assignments"] == [
        {
            "location_id": "B001",
            "lit_location": "B001",
            "color": [40, 120, 255],
        }
    ]


def test_assistant_agent_lights_bare_item_search_results(tmp_path, monkeypatch) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [{"size": "medium", "bin_count": 1, "spans": [1]}],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    monkeypatch.setattr(
        "searchable_storage_led.controller.WledWriter.write_pixels",
        lambda self, pixels, config: None,
    )
    monkeypatch.setattr(
        "searchable_storage_led.api.run_agent",
        lambda *args, **kwargs: AgentResult(
            message="Heat shrink tubing assortment — Main Cabinet > B001.",
            trace=[
                {
                    "tool": "search_items",
                    "arguments": {"query": "heat shrink"},
                    "result": [
                        {
                            "name": "Heat shrink tubing assortment",
                            "item_id": 2,
                            "location_id": "B001",
                            "nearest_lit_location_id": "B001",
                            "path": [{"id": "B001", "label": "B001"}],
                        }
                    ],
                }
            ],
        ),
    )
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/chat",
        json={"text": "heat shrink", "mode": "agent", "source": "ui"},
    ).json()

    assert response["action"] == "found"
    assert response["lit"] is True
    assert response["light_assignments"] == [
        {
            "location_id": "B001",
            "lit_location": "B001",
            "color": [255, 0, 0],
        }
    ]


def test_assistant_chat_voice_uses_command_after_last_wake_word(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [{"size": "small", "bin_count": 1, "spans": [1]}],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post("/inventory", json={"name": "M3 screws", "bin_id": "B001"})

    response = client.post(
        "/assistant/chat",
        json={
            "text": "M3 screws. Magpie, show me M3 screws",
            "source": "voice",
        },
    ).json()

    assert response["mode"] == "find"
    assert response["results"][0]["name"] == "M3 screws"
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert latest["command"] == "M3 screws"


def test_assistant_chat_answers_inventory_status_without_creating_item(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [{"size": "small", "bin_count": 1, "spans": [1]}],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post("/inventory", json={"name": "M4 washers", "bin_id": "B001"})

    response = client.post(
        "/assistant/chat",
        json={"text": "what is the status of the inventory", "source": "voice"},
    ).json()

    assert response["mode"] == "clarify"
    assert response["decision"]["action"] == "voice_scope_ui_required"
    assert "M4 washers" not in response["message"]
    assert len(client.get("/inventory").json()["items"]) == 1


def test_assistant_chat_treats_spoken_compound_retrieval_as_find(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post("/inventory", json={"name": "M3 screws", "bin_id": "B001"})
    client.post("/inventory", json={"name": "M4 T-nuts", "bin_id": "B002"})

    response = client.post(
        "/assistant/chat",
        json={
            "text": "please retrieve the M3 screws and the M4 T-nuts from the top drawer",
            "source": "voice",
        },
    ).json()

    assert response["mode"] == "find"
    assert response["raw"]["action"] == "found"
    assert {result["name"] for result in response["results"]} == {
        "M3 screws",
        "M4 T-nuts",
    }
    assert "M3 screws is in" in response["message"]
    assert "M4 T-nuts is in" in response["message"]
    assert len(client.get("/inventory").json()["items"]) == 2


def test_assistant_chat_treats_spoken_grab_list_as_find(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post("/inventory", json={"name": "M3 screws", "bin_id": "B001"})
    client.post("/inventory", json={"name": "M4 T-nuts", "bin_id": "B002"})

    response = client.post(
        "/assistant/chat",
        json={
            "text": "grab the M3 screws and M4 T-nuts",
            "source": "voice",
        },
    ).json()

    assert response["mode"] == "find"
    assert response["raw"]["action"] == "found"
    assert {result["name"] for result in response["results"]} == {
        "M3 screws",
        "M4 T-nuts",
    }
    assert len(client.get("/inventory").json()["items"]) == 2


def test_assistant_chat_clarifies_constraint_only_voice_fragment(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [{"size": "large", "bin_count": 1, "spans": [1]}],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/chat",
        json={"text": "No, it has to be a large bin", "source": "voice"},
    ).json()

    assert response["mode"] == "clarify"
    assert response["decision"]["action"] == "voice_scope_ui_required"
    assert response["message"] == VOICE_SCOPE_MESSAGE
    assert client.get("/inventory").json()["items"] == []


def test_assistant_chat_clarifies_wake_word_only(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [{"size": "small", "bin_count": 1, "spans": [1]}],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/chat",
        json={"text": "Magpie", "source": "voice"},
    ).json()

    assert response["mode"] == "clarify"
    assert response["decision"]["action"] == "clarify"
    assert client.get("/inventory").json()["items"] == []


def test_assistant_chat_putaway_uses_low_friction_ask_flow(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    response = client.post("/assistant/chat", json={"text": "put away M4 T-nuts"})

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "ask"
    assert body["decision"]["action"] == "put_away"
    assert body["decision"]["destination"]["id"] == "B001"
    assert body["confirmation"] == body["decision"]["confirmation"]
    assert body["decision"]["confirmation"]["schema"] == "magpie.confirmation.v1"
    assert body["decision"]["confirmation"]["explanation"]["label"] == (
        "Why this location?"
    )
    assert body["decision"]["confirmation"]["explanation"]["summary"]
    assert body["decision"]["confirmation"]["rows"][0] == {
        "index": 0,
        "tool": "put_item",
        "summary": "Store 1 x M4 T-nuts",
        "destination": {
            "id": "B001",
            "path": [
                {"id": "main-cabinet", "label": "Test"},
                {"id": "B001", "label": "B001"},
            ],
            "label": "Test › B001",
            "light_location_id": "B001",
        },
        "color": {"name": "Red", "rgb": [255, 0, 0]},
        "light": {"location_id": "B001", "status": "unavailable"},
    }
    assert body["decision"]["plan"] == [
        {
            "tool": "put_item",
            "arguments": {
                "item": "M4 T-nuts",
                "location_id": "B001",
                "quantity": 1.0,
                "unit": "each",
                "size_class": "small",
                "dimensions": {"thread_diameter_mm": 4.0},
            },
        }
    ]
    assert client.get("/inventory").json()["items"] == []
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert latest["route"] == "/assistant/chat"
    assert latest["command"] == "M4 T-nuts"
    assert latest["response"]["confirmation"] == body["confirmation"]

    canceled = client.post(
        "/assistant/cancel",
        json={
            "command": "Put away M4 T-nuts",
            "source_interaction_id": body["decision"]["interaction_id"],
        },
    ).json()

    assert canceled["active_find_removed"] is True
    assert canceled["active_finds"] == []
    assert client.get("/lights/active").json()["active_finds"] == []


def test_voice_cancel_removes_pending_putaway_light(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "searchable_storage_led.controller.WledWriter.write_pixels",
        lambda self, pixels, config: None,
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="wled",
                wled_url="http://127.0.0.1:1",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    proposed = client.post(
        "/assistant/chat",
        json={"text": "put away M4 T-nuts"},
    ).json()

    assert proposed["decision"]["active_finds"]

    canceled = client.post("/assistant/voice/cancel").json()

    assert canceled["ok"] is True
    assert canceled["active_find_removed"] is True
    assert canceled["active_finds"] == []
    assert client.get("/lights/active").json()["active_finds"] == []


def test_assistant_chat_putaway_avoids_unrelated_dedicated_location(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="wled",
                wled_url="http://127.0.0.1:1",
                layout_path=str(layout_path),
                database_path=str(db_path),
            )
        )
    )
    LocationStore(db_path).upsert(
        Location(
            id="allen-keys-and-wrenches-bin",
            type="bin",
            label="Allen Keys and Wrenches Bin",
            parent_id="main-cabinet",
            capacity_class="medium",
        )
    )

    body = client.post(
        "/assistant/chat",
        json={"text": "Let's put away LED strip scraps.", "source": "voice"},
    ).json()

    assert body["decision"]["action"] == "put_away"
    assert body["decision"]["destination"]["id"] == "B001"
    assert body["decision"]["destination"]["id"] != "allen-keys-and-wrenches-bin"


def test_assistant_chat_putaway_extracts_leading_quantity(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="wled",
                wled_url="http://127.0.0.1:1",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )

    decision = client.post(
        "/assistant/chat", json={"text": "put away 12 M4 washers in B001"}
    ).json()["decision"]

    assert decision["plan"][0]["arguments"] == {
        "item": "M4 Washers",
        "location_id": "B001",
        "quantity": 12.0,
        "unit": "each",
        "size_class": "small",
        "dimensions": {"thread_diameter_mm": 4.0},
    }


def test_assistant_chat_putaway_understands_continuation_quantities(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    store = InventoryStore(database_path)
    item_id = store.upsert_item(name="M4 Bolts", category="fasteners")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=12, unit="each")
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=2,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(database_path),
            )
        )
    )

    for command, quantity in (
        ("put away 4 more M4 bolts", 4.0),
        ("put away another 6 M4 bolts", 6.0),
        ("put away add 10 M4 bolts", 10.0),
        ("put away 2 more packs of the M4 bolts", 2.0),
    ):
        decision = client.post("/assistant/chat", json={"text": command}).json()[
            "decision"
        ]
        arguments = decision["plan"][0]["arguments"]
        assert arguments["item"] == "M4 Bolts"
        assert arguments["location_id"] == "B001"
        assert arguments["quantity"] == quantity
        assert arguments["quantity_mode"] == "add"

    different_variant = client.post(
        "/assistant/chat",
        json={"text": "put away 2 more M5 bolts"},
    ).json()["decision"]
    different_arguments = different_variant["plan"][0]["arguments"]
    assert different_arguments["item"] == "M5 Bolts"
    assert different_arguments["location_id"] == "B002"
    assert different_arguments["quantity"] == 2.0


def test_assistant_chat_putaway_does_not_treat_measurement_as_quantity(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="wled",
                wled_url="http://127.0.0.1:1",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )

    args = client.post(
        "/assistant/chat", json={"text": "put away 10 mm screws"}
    ).json()["decision"]["plan"][0]["arguments"]

    assert args["item"] == "10 Mm Screws"
    assert args["quantity"] == 1.0


def test_assistant_chat_putaway_existing_item_proposes_same_location(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post(
        "/inventory",
        json={
            "name": "M4 x 12 mm button head hex screws",
            "aliases": ["M4 button head screws"],
            "bin_id": "B001",
            "quantity": 4,
            "unit": "lot",
        },
    )

    body = client.post(
        "/assistant/chat",
        json={"text": "put away M4 button head screws", "source": "voice"},
    ).json()

    decision = body["decision"]
    assert decision["action"] == "put_away"
    assert decision["existing_item"] is True
    assert decision["destination"]["id"] == "B001"
    assert decision["plan"] == [
        {
            "tool": "put_item",
            "arguments": {
                "item": "M4 x 12 mm button head hex screws",
                "location_id": "B001",
                "quantity": 1.0,
                "unit": "lot",
                "quantity_mode": "add",
            },
        }
    ]
    assert "Put them with the existing" in decision["message"]
    inventory = client.get("/inventory").json()["items"]
    assert inventory[0]["quantity"] == 4.0

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "put away M4 button head screws",
            "plan": decision["plan"],
        },
    ).json()
    assert confirmed["applied"][0]["previous_quantity"] == 4.0
    assert confirmed["applied"][0]["quantity"] == 5.0
    assert client.get("/inventory").json()["items"][0]["quantity"] == 5.0

    undone = client.post("/assistant/undo").json()
    assert undone["undone"][0]["restored_item"] == confirmed["applied"][0]["item_id"]
    assert client.get("/inventory").json()["items"][0]["quantity"] == 4.0


def test_assistant_chat_putaway_keeps_intent_after_local_typo_correction(
    tmp_path, monkeypatch
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post(
        "/inventory",
        json={
            "name": "M3 flush mount screws",
            "bin_id": "B001",
            "quantity": 12,
            "unit": "each",
        },
    )
    monkeypatch.setattr("searchable_storage_led.api._llm_key_present", lambda _cfg: True)

    def unexpected_enrichment(*args, **kwargs):
        raise AssertionError("text-only typo recovery must not call model enrichment")

    monkeypatch.setattr(
        "searchable_storage_led.api.enrich_item_draft",
        unexpected_enrichment,
    )

    body = client.post(
        "/assistant/chat",
        json={
            "text": "put away M3 flush mont skrews",
            "source": "voice",
        },
    ).json()

    decision = body["decision"]
    assert decision["action"] == "put_away"
    assert decision["existing_item"] is True
    assert decision["destination"]["id"] == "B001"
    assert decision["plan"][0]["tool"] == "put_item"
    assert decision["plan"][0]["arguments"]["quantity_mode"] == "add"


def test_assistant_chat_text_putaway_does_not_call_model_enrichment(
    tmp_path, monkeypatch
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="none",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    monkeypatch.setattr("searchable_storage_led.api._llm_key_present", lambda _cfg: True)

    def unexpected_enrichment(*args, **kwargs):
        raise AssertionError("text-only put-away must not call model enrichment")

    monkeypatch.setattr(
        "searchable_storage_led.api.enrich_item_draft",
        unexpected_enrichment,
    )
    client = TestClient(create_app(cfg))

    body = client.post(
        "/assistant/chat",
        json={"text": "put away M3 flush mount screws", "source": "voice"},
    ).json()

    decision = body["decision"]
    assert decision["action"] == "put_away"
    assert decision["item"]["name"] == "M3 Flush Mount Screws"
    assert decision["item"]["category"] == "fasteners"
    assert decision["plan"][0]["arguments"]["item"] == "M3 Flush Mount Screws"


def test_assistant_chat_typo_match_does_not_merge_conflicting_hardware_dimensions(
    tmp_path, monkeypatch
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=2,
        backend="none",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    monkeypatch.setattr("searchable_storage_led.api._llm_key_present", lambda _cfg: True)

    def unexpected_enrichment(*args, **kwargs):
        raise AssertionError("text-only put-away must stay local")

    monkeypatch.setattr(
        "searchable_storage_led.api.enrich_item_draft",
        unexpected_enrichment,
    )
    client = TestClient(create_app(cfg))
    client.post(
        "/inventory",
        json={"name": "M4 x 10 mm screws", "bin_id": "B001"},
    )

    body = client.post(
        "/assistant/chat",
        json={"text": "put away M4 x 12 mm skrews", "source": "voice"},
    ).json()

    decision = body["decision"]
    assert decision.get("existing_item") is not True
    assert decision["destination"]["id"] == "B002"
    assert decision["plan"][0]["arguments"]["item"] == "M4 X 12 Mm Skrews"


def test_assistant_chat_photo_putaway_keeps_model_enrichment(
    tmp_path, monkeypatch
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "large", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    monkeypatch.setattr("searchable_storage_led.api._llm_key_present", lambda _cfg: True)
    calls = []

    def enrich(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "name": "Blue Pry Tool",
            "category": "tools",
            "aliases": ["nylon spudger"],
            "unit": "each",
            "quantity": 1,
        }

    monkeypatch.setattr("searchable_storage_led.api.enrich_item_draft", enrich)
    client = TestClient(create_app(cfg))
    photo_id = client.post(
        "/photos",
        json={
            "filename": "pry-tool.jpg",
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(b"image").decode("ascii"),
        },
    ).json()["photo"]["id"]

    body = client.post(
        "/assistant/chat",
        json={
            "text": "put this away",
            "photo_ids": [photo_id],
            "source": "voice",
        },
    ).json()

    assert len(calls) == 1, body
    assert "item" in body["decision"], body
    assert body["decision"]["item"]["name"] == "Blue Pry Tool"
    assert body["decision"]["plan"][0]["arguments"]["item"] == "Blue Pry Tool"


def test_photo_backed_move_to_existing_place_uses_putaway_fast_path(
    tmp_path, monkeypatch
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="none",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    monkeypatch.setattr("searchable_storage_led.api._llm_key_present", lambda _cfg: False)
    client = TestClient(create_app(cfg))
    locations = LocationStore(db_path)
    locations.upsert(
        Location(
            id="main-cabinet",
            type="cabinet",
            label="Main Cabinet",
            accepts_items=False,
        )
    )
    locations.upsert(
        Location(
            id="small-hardware-bin",
            type="bin",
            parent_id="main-cabinet",
            label="Small Hardware Bin",
            capacity_class="small",
        )
    )
    photo_id = client.post(
        "/photos",
        json={
            "filename": "m3-screws.jpg",
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(b"image").decode("ascii"),
        },
    ).json()["photo"]["id"]
    command = (
        "Move the small M3 flush mount screws into the existing small hardware "
        "bin and keep the photo with the inventory record"
    )

    proposed = client.post(
        "/assistant/chat",
        json={"text": command, "photo_ids": [photo_id]},
    )

    assert proposed.status_code == 200
    body = proposed.json()
    assert body["action"] == "put_away"
    assert body["plan"] == [
        {
            "tool": "put_item",
            "arguments": {
                "item": "Small M3 Flush Mount Screws",
                "location_id": "small-hardware-bin",
                "quantity": 1.0,
                "unit": "each",
                "size_class": "small",
                "dimensions": {"thread_diameter_mm": 3.0},
            },
        }
    ]
    pending = InventoryStore(db_path).assistant_interaction(body["interaction_id"])
    assert pending is not None
    assert pending["command"] == command
    assert pending["photo_ids"] == [photo_id]

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": command,
            "source_interaction_id": body["interaction_id"],
            "plan": body["plan"],
        },
    )

    assert confirmed.status_code == 200
    receipt = confirmed.json()["receipt"]
    assert receipt["photos"] == {"uploaded": 1, "attached": 1, "pending": 0}
    stored = InventoryStore(db_path).exact_match("Small M3 Flush Mount Screws")
    assert len(stored) == 1
    assert stored[0].bin_id == "small-hardware-bin"
    assert InventoryStore(db_path).photo(photo_id).item_id == stored[0].item_id


def test_existing_item_move_keeps_proposal_photo_and_voice_scope(
    tmp_path, monkeypatch
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=2,
        backend="none",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    monkeypatch.setattr("searchable_storage_led.api._llm_key_present", lambda _cfg: False)
    client = TestClient(create_app(cfg))
    locations = LocationStore(db_path)
    locations.upsert(
        Location(
            id="main-cabinet",
            type="cabinet",
            label="Main Cabinet",
            accepts_items=False,
        )
    )
    locations.upsert(
        Location(
            id="B001",
            type="bin",
            parent_id="main-cabinet",
            label="Source Bin",
            capacity_class="small",
        )
    )
    locations.upsert(
        Location(
            id="B002",
            type="bin",
            parent_id="main-cabinet",
            label="Small Hardware Bin",
            capacity_class="small",
        )
    )
    inventory = InventoryStore(db_path)
    item_id = inventory.upsert_item(name="M3 Flush Mount Screws")
    inventory.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")
    photo_id = client.post(
        "/photos",
        json={
            "filename": "moved-screws.jpg",
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(b"image").decode("ascii"),
        },
    ).json()["photo"]["id"]
    command = (
        "Move M3 flush mount screws into the existing small hardware bin "
        "and attach the photo to the item"
    )

    voice = client.post(
        "/assistant/chat",
        json={"text": command, "photo_ids": [photo_id], "source": "voice"},
    ).json()
    assert voice["decision"]["action"] == "voice_scope_ui_required"

    proposed = client.post(
        "/assistant/chat",
        json={"text": command, "photo_ids": [photo_id]},
    )

    assert proposed.status_code == 200
    body = proposed.json()
    assert body["plan"] == [
        {
            "tool": "move_item",
            "arguments": {
                "item_id": item_id,
                "item": "M3 Flush Mount Screws",
                "from_location_id": "B001",
                "to_location_id": "B002",
            },
        }
    ]
    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": command,
            "source_interaction_id": body["interaction_id"],
            "plan": body["plan"],
        },
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["receipt"]["summary"] == (
        "Moved M3 Flush Mount Screws to Main Cabinet › Small Hardware Bin."
    )
    assert InventoryStore(db_path).exact_match("M3 Flush Mount Screws")[0].bin_id == "B002"
    assert InventoryStore(db_path).photo(photo_id).item_id == item_id


def test_assistant_chat_putaway_clarifies_underspecified_hardware_variant(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=2,
                backend="wled",
                wled_url="http://127.0.0.1:1",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    for name, bin_id in [
        ("M4 x 12 mm button head screws", "B001"),
        ("M4 x 20 mm button head screws", "B002"),
    ]:
        client.post(
            "/inventory",
            json={"name": name, "bin_id": bin_id, "quantity": 10, "unit": "pieces"},
        )

    ambiguous = client.post(
        "/assistant/chat",
        json={"text": "put away M4 button head screws"},
    ).json()

    assert ambiguous["decision"]["action"] == "clarify_item_identity"
    assert ambiguous["decision"]["plan"] == []
    assert ambiguous["decision"]["lit"] is False
    assert {item["name"] for item in ambiguous["decision"]["identity_candidates"]} == {
        "M4 x 12 mm button head screws",
        "M4 x 20 mm button head screws",
    }

    exact = client.post(
        "/assistant/chat",
        json={"text": "put away M4 x 20 mm button head screw"},
    ).json()
    assert exact["decision"]["action"] == "put_away"
    assert exact["decision"]["existing_item"] is True
    assert exact["decision"]["destination"]["id"] == "B002"
    assert exact["decision"]["plan"][0]["arguments"]["item"] == (
        "M4 x 20 mm button head screws"
    )


def test_assistant_chat_distinguishes_imperial_flush_screw_lengths(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=2,
                backend="wled",
                wled_url="http://127.0.0.1:1",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    for name, bin_id in [
        ('#8 x 1/2" flush mount screws', "B001"),
        ('#8 x 3/4" flush mount screws', "B002"),
    ]:
        client.post(
            "/inventory",
            json={"name": name, "bin_id": bin_id, "quantity": 10, "unit": "pieces"},
        )

    ambiguous = client.post(
        "/assistant/chat",
        json={"text": "put away #8 flush mount screws"},
    ).json()

    assert ambiguous["decision"]["action"] == "clarify_item_identity"
    assert {item["name"] for item in ambiguous["decision"]["identity_candidates"]} == {
        '#8 x 1/2" flush mount screws',
        '#8 x 3/4" flush mount screws',
    }

    exact = client.post(
        "/assistant/chat",
        json={"text": "put away #8 x 3/4 inch flush mount screw"},
    ).json()

    assert exact["decision"]["action"] == "put_away"
    assert exact["decision"]["existing_item"] is True
    assert exact["decision"]["destination"]["id"] == "B002"
    assert exact["decision"]["plan"][0]["arguments"]["item"] == (
        '#8 x 3/4" flush mount screws'
    )


def test_assistant_chat_putaway_uses_current_location_as_target(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post(
        "/locations",
        json={
            "type": "box",
            "label": "Bench Box",
            "capacity_class": "medium",
        },
    )

    response = client.post(
        "/assistant/chat",
        json={
            "text": "put away spare nozzles",
            "current_location_id": "bench-box",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "ask"
    assert body["decision"]["action"] == "put_away"
    assert body["decision"]["destination"]["id"] == "bench-box"
    assert "Bench Box" in body["decision"]["message"]


def test_resumed_putaway_keeps_the_visual_destination_authoritative(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    assert client.post(
        "/locations",
        json={
            "id": "chosen-box",
            "label": "Chosen Box",
            "type": "box",
            "capacity_class": "tiny",
            "accepts_items": True,
        },
    ).status_code == 200
    assert client.post(
        "/inventory",
        json={"name": "M6 screws", "bin_id": "B001", "quantity": 10},
    ).status_code == 200

    ordinary = client.post(
        "/assistant/chat",
        json={
            "text": "put away M6 screws",
            "current_location_id": "chosen-box",
        },
    ).json()
    resumed = client.post(
        "/assistant/chat",
        json={
            "text": "put away M6 screws",
            "current_location_id": "chosen-box",
            "destination_locked": True,
        },
    ).json()

    assert ordinary["decision"]["destination"]["id"] == "B001"
    assert resumed["decision"]["destination"]["id"] == "chosen-box"
    assert resumed["decision"]["plan"][0]["arguments"]["location_id"] == (
        "chosen-box"
    )
    assert resumed["decision"]["placement"]["reasons"] == [
        "using the destination selected in Places"
    ]
    assert "destination you selected" in resumed["decision"]["message"]


def test_assistant_chat_putaway_uses_inferred_dimensions_for_physical_fit(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=2,
                backend="wled",
                wled_url="http://127.0.0.1:1",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    client.post(
        "/locations/B001/update",
        json={"internal_dimensions": {"width_mm": 3, "depth_mm": 10, "height_mm": 10}},
    )
    client.post(
        "/locations/B002/update",
        json={"internal_dimensions": {"width_mm": 10, "depth_mm": 30, "height_mm": 10}},
    )

    response = client.post(
        "/assistant/chat",
        json={"text": "put away M4 x 20 mm button head screw"},
    )

    assert response.status_code == 200
    decision = response.json()["decision"]
    assert decision["action"] == "put_away"
    assert decision["destination"]["id"] == "B002"
    assert decision["item"]["dimensions"] == {
        "thread_diameter_mm": 4.0,
        "length_mm": 20.0,
    }

    imperial_response = client.post(
        "/assistant/chat",
        json={"text": "put away #8 x 1/2 inch flush mount screws"},
    )

    assert imperial_response.status_code == 200
    imperial = imperial_response.json()["decision"]
    assert imperial["action"] == "put_away"
    assert imperial["destination"]["id"] == "B002"
    assert imperial["item"]["dimensions"] == {
        "thread_diameter_mm": 4.166,
        "length_mm": 12.7,
    }


def test_assistant_chat_putaway_uses_learned_size_rule_for_destination(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                    {"size": "medium", "bin_count": 1, "spans": [2]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=3,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    store.learn_size_rule(
        rule_text="Jewelry boxes are medium.",
        trigger_terms=["jewelry", "box"],
        size_class="medium",
    )
    client = TestClient(create_app(cfg))

    response = client.post("/assistant/chat", json={"text": "put away jewelry box"})

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "ask"
    assert body["decision"]["action"] == "put_away"
    assert body["decision"]["destination"]["id"] == "B003"
    assert body["decision"]["item"]["size_class"] == "medium"
    assert body["decision"]["size"] == {
        "class": "medium",
        "reason": "learned size rule: Jewelry boxes are medium.",
    }
    assert body["decision"]["placement"]["reason"] == (
        "this follows your rule: Jewelry boxes are medium."
    )
    assert body["decision"]["message"] == (
        "File Jewelry Box in Test › B003. Confirm to save it."
    )
    assert body["decision"]["confirmation"]["explanation"] == {
        "label": "Why this location?",
        "summary": "this follows your rule: Jewelry boxes are medium.",
    }
    adherence = body["decision"]["learning"]["rule_adherence"]
    assert adherence == [
        {
            "rule_id": 1,
            "rule_type": "size",
            "rule_text": "Jewelry boxes are medium.",
            "expected": "medium",
            "actual": "medium",
            "followed": True,
        }
    ]
    summary = client.get("/assistant/learning/adherence").json()
    assert summary["count"] == 1
    assert summary["followed"] == 1
    assert summary["not_followed"] == 0
    assert summary["rate"] == 1.0


def test_assistant_chat_putaway_honors_explicit_size_request(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                    {"size": "medium", "bin_count": 1, "spans": [2]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=3,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/chat",
        json={
            "text": "let's put these magnets in a medium bin",
            "source": "voice",
        },
    )

    assert response.status_code == 200
    decision = response.json()["decision"]
    assert decision["action"] == "put_away"
    assert decision["destination"]["id"] == "B003"
    assert decision["item"]["size_class"] == "medium"
    assert decision["size"] == {
        "class": "medium",
        "reason": "requested medium container",
    }


def test_assistant_followup_learns_size_correction_from_filed_context(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="Jewelry Box")
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/followup",
        json={
            "text": "no that should be medium",
            "context": {
                "kind": "filed",
                "item": {"name": "Jewelry Box"},
                "saved_item_id": item_id,
                "destination": {"id": "B001"},
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["handled"] is True
    assert body["action"] == "learned_size_correction"
    assert body["size_class"] == "medium"
    assert body["rule_text"] == "Jewelry Box should be medium."
    proposed = body["proposed_rule"]
    assert proposed["rule_type"] == "size"
    assert proposed["review_status"] == "promoted"
    assert proposed["provisional"] is True
    assert store.item_size_class("Jewelry Box") == "medium"
    rule = store.matching_size_rules("small jewelry box")[0]
    assert rule.size_class == "medium"
    assert rule.source_command == "no that should be medium"
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert latest["route"] == "/assistant/followup"
    assert latest["response"]["action"] == "learned_size_correction"
    example = client.get(
        f"/assistant/learning/examples?limit=10&kind=correction"
    ).json()["examples"][0]
    assert example["id"] == proposed["example_id"]
    assert example["review_status"] == "promoted"
    assert example["promotable"] is False
    assert example["forgettable"] is True
    assert example["parsed_action"][0]["tool"] == "learn_size_rule"
    rejected = client.delete(
        f"/assistant/learning/examples/{proposed['example_id']}"
    ).json()
    assert rejected["rolled_back"] == [
        {"rule_id": proposed["rule_id"], "rule_type": "size"}
    ]
    assert store.matching_size_rules("small jewelry box") == []


def test_assistant_followup_moves_item_and_learns_placement_correction(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="M4 T-nuts")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=2, unit="each")
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/followup",
        json={
            "text": "actually put it in B002",
            "context": {
                "kind": "filed",
                "item": {"name": "M4 T-nuts"},
                "saved_item_id": item_id,
                "destination": {"id": "B001"},
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["handled"] is True
    assert body["action"] == "learned_placement_correction"
    assert body["from_location_id"] == "B001"
    assert body["to_location_id"] == "B002"
    assert body["destination"]["id"] == "B002"
    assert body["lit"] is False
    assert body["proposed_rule"]["rule_type"] == "placement"
    assert body["proposed_rule"]["review_status"] == "promoted"
    assert store.items_in_location("B001") == []
    moved = store.items_in_location("B002")
    assert moved[0].item_id == item_id
    assert moved[0].quantity == 2
    rule = store.matching_rules("M4 T-nuts")[0]
    assert rule.target_location_id == "B002"
    assert rule.source_command == "actually put it in B002"
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert latest["route"] == "/assistant/followup"
    assert latest["response"]["action"] == "learned_placement_correction"


def test_assistant_followup_can_move_item_with_existing_related_item(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    tnut_id = store.upsert_item(name="M4 T-nuts")
    nut_id = store.upsert_item(name="M4 nuts")
    store.put_inventory(item_id=tnut_id, bin_id="B001", quantity=2, unit="each")
    store.put_inventory(item_id=nut_id, bin_id="B002", quantity=10, unit="each")
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/followup",
        json={
            "text": "actually put that with the M4 nuts",
            "context": {
                "kind": "filed",
                "item": {"name": "M4 T-nuts"},
                "saved_item_id": tnut_id,
                "destination": {"id": "B001"},
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["handled"] is True
    assert body["action"] == "learned_placement_correction"
    assert body["to_location_id"] == "B002"
    assert store.items_in_location("B001") == []
    assert {item.name for item in store.items_in_location("B002")} == {"M4 T-nuts", "M4 nuts"}
    rule = store.matching_rules("M4 T-nuts")[0]
    assert rule.target_location_id == "B002"
    assert rule.source_command == "actually put that with the M4 nuts"


def test_assistant_followup_asks_when_related_item_destination_is_ambiguous(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 3,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 3, "spans": [1, 1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=3,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    washer_id = store.upsert_item(name="M4 washers")
    m4_nuts_id = store.upsert_item(name="M4 nuts")
    m5_nuts_id = store.upsert_item(name="M5 nuts")
    store.put_inventory(item_id=washer_id, bin_id="B001", quantity=4, unit="each")
    store.put_inventory(item_id=m4_nuts_id, bin_id="B002", quantity=10, unit="each")
    store.put_inventory(item_id=m5_nuts_id, bin_id="B003", quantity=8, unit="each")
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/followup",
        json={
            "text": "actually put that with the nuts",
            "context": {
                "kind": "filed",
                "item": {"name": "M4 washers"},
                "saved_item_id": washer_id,
                "destination": {"id": "B001"},
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["handled"] is True
    assert body["action"] == "clarify_placement_destination"
    assert {candidate["location_id"] for candidate in body["candidates"]} == {"B002", "B003"}
    assert [item.name for item in store.items_in_location("B001")] == ["M4 washers"]
    assert store.matching_rules("M4 washers") == []


def test_assistant_followup_accepts_short_location_answer_after_clarification(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    washer_id = store.upsert_item(name="M4 washers")
    store.put_inventory(item_id=washer_id, bin_id="B001", quantity=4, unit="each")
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/followup",
        json={
            "text": "B002",
            "context": {
                "kind": "filed",
                "item": {"name": "M4 washers"},
                "saved_item_id": washer_id,
                "destination": {"id": "B001"},
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["handled"] is True
    assert body["action"] == "learned_placement_correction"
    assert body["to_location_id"] == "B002"
    assert store.items_in_location("B001") == []
    assert store.items_in_location("B002")[0].name == "M4 washers"


def test_assistant_followup_accepts_short_related_item_answer(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    washer_id = store.upsert_item(name="M4 washers")
    nut_id = store.upsert_item(name="M4 nuts")
    store.put_inventory(item_id=washer_id, bin_id="B001", quantity=4, unit="each")
    store.put_inventory(item_id=nut_id, bin_id="B002", quantity=10, unit="each")
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/followup",
        json={
            "text": "M4 nuts",
            "context": {
                "kind": "filed",
                "item": {"name": "M4 washers"},
                "saved_item_id": washer_id,
                "destination": {"id": "B001"},
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["handled"] is True
    assert body["action"] == "learned_placement_correction"
    assert body["to_location_id"] == "B002"
    assert {item.name for item in store.items_in_location("B002")} == {"M4 washers", "M4 nuts"}


def test_assistant_followup_updates_quantity_from_filed_context(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="M4 T-nuts")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=2, unit="each")
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/followup",
        json={
            "text": "actually there are 4",
            "context": {
                "kind": "filed",
                "item": {"name": "M4 T-nuts"},
                "saved_item_id": item_id,
                "destination": {"id": "B001"},
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["handled"] is True
    assert body["action"] == "learned_quantity_correction"
    assert body["quantity"] == 4
    assert body["unit"] == "each"
    item = store.items_in_location("B001")[0]
    assert item.item_id == item_id
    assert item.quantity == 4
    assert item.unit == "each"
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert latest["route"] == "/assistant/followup"
    assert latest["response"]["action"] == "learned_quantity_correction"


def test_assistant_followup_updates_quantity_and_unit_from_filed_context(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="Heat-set inserts")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="pack")
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/followup",
        json={
            "text": "make it 3 packs",
            "context": {
                "kind": "filed",
                "item": {"name": "Heat-set inserts"},
                "saved_item_id": item_id,
                "destination": {"id": "B001"},
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["handled"] is True
    assert body["action"] == "learned_quantity_correction"
    assert body["quantity"] == 3
    assert body["unit"] == "packs"
    item = store.items_in_location("B001")[0]
    assert item.quantity == 3
    assert item.unit == "packs"


def test_assistant_followup_renames_filed_item_and_keeps_alias(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="Generic Hardware")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=2, unit="each")
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/followup",
        json={
            "text": "actually it's M4 T-nuts",
            "context": {
                "kind": "filed",
                "item": {"name": "Generic Hardware"},
                "saved_item_id": item_id,
                "destination": {"id": "B001"},
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["handled"] is True
    assert body["action"] == "learned_identity_correction"
    assert body["item"] == "M4 T-nuts"
    assert body["previous_name"] == "Generic Hardware"
    item = store.items_in_location("B001")[0]
    assert item.item_id == item_id
    assert item.name == "M4 T-nuts"
    assert "Generic Hardware" in item.aliases
    assert store.search("Generic Hardware")[0].name == "M4 T-nuts"
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert latest["route"] == "/assistant/followup"
    assert latest["response"]["action"] == "learned_identity_correction"


def test_assistant_followup_identity_correction_does_not_steal_size_word(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="Jewelry Box")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/followup",
        json={
            "text": "actually it's medium",
            "context": {
                "kind": "filed",
                "item": {"name": "Jewelry Box"},
                "saved_item_id": item_id,
                "destination": {"id": "B001"},
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["handled"] is True
    assert body["action"] == "learned_size_correction"
    assert store.items_in_location("B001")[0].name == "Jewelry Box"
    assert store.item_size_class("Jewelry Box") == "medium"


def test_assistant_followup_updates_category_from_filed_context(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="M4 T-nuts", category="hardware")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=2, unit="each")
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/followup",
        json={
            "text": "actually category is fasteners",
            "context": {
                "kind": "filed",
                "item": {"name": "M4 T-nuts"},
                "saved_item_id": item_id,
                "destination": {"id": "B001"},
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["handled"] is True
    assert body["action"] == "learned_category_correction"
    assert body["category"] == "fasteners"
    item = store.items_in_location("B001")[0]
    assert item.category == "fasteners"
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert latest["route"] == "/assistant/followup"
    assert latest["response"]["action"] == "learned_category_correction"


def test_assistant_followup_adds_alias_from_filed_context(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="M4 T-nuts")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=2, unit="each")
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/followup",
        json={
            "text": "also called tee nuts",
            "context": {
                "kind": "filed",
                "item": {"name": "M4 T-nuts"},
                "saved_item_id": item_id,
                "destination": {"id": "B001"},
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["handled"] is True
    assert body["action"] == "learned_alias_correction"
    assert body["alias"] == "tee nuts"
    assert "tee nuts" in body["aliases"]
    assert store.search("tee nuts")[0].name == "M4 T-nuts"
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert latest["route"] == "/assistant/followup"
    assert latest["response"]["action"] == "learned_alias_correction"


def test_assistant_followup_category_correction_does_not_steal_size_word(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="Jewelry Box", category="storage")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/followup",
        json={
            "text": "category is medium",
            "context": {
                "kind": "filed",
                "item": {"name": "Jewelry Box"},
                "saved_item_id": item_id,
                "destination": {"id": "B001"},
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["handled"] is True
    assert body["action"] == "learned_size_correction"
    item = store.items_in_location("B001")[0]
    assert item.category == "storage"
    assert store.item_size_class("Jewelry Box") == "medium"


def test_assistant_chat_pending_agent_plan_can_be_voice_confirmed(tmp_path, monkeypatch) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )

    def fake_run_agent(*args, **kwargs):
        return SimpleNamespace(
            message="Plan ready.",
            plan=[
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "M4 T-nuts",
                        "location_id": "B001",
                        "quantity": 2,
                    },
                }
            ],
            trace=[],
        )

    monkeypatch.setattr("searchable_storage_led.api.run_agent", fake_run_agent)
    client = TestClient(create_app(cfg))

    proposed = client.post(
        "/assistant/chat",
        json={"text": "move M4 T-nuts to B001", "mode": "agent", "source": "web"},
    ).json()
    confirmed = client.post("/assistant/voice/confirm").json()

    assert proposed["route"] == "/assistant/chat"
    assert proposed["interaction_id"] == confirmed["source_interaction_id"]
    assert confirmed["ok"] is True
    assert confirmed["message"] == "Confirmed. Stored M4 T-nuts in Test › B001."
    assert confirmed["confirmation_interaction_id"] > proposed["interaction_id"]
    interactions = client.get("/assistant/interactions?limit=10").json()["interactions"]
    source = next(row for row in interactions if row["id"] == proposed["interaction_id"])
    confirmation = next(
        row for row in interactions
        if row["id"] == confirmed["confirmation_interaction_id"]
    )
    assert (
        source["response"]["confirmation_response"]["confirmation_interaction_id"]
        == confirmed["confirmation_interaction_id"]
    )
    assert (
        confirmation["response"]["confirmation_interaction_id"]
        == confirmed["confirmation_interaction_id"]
    )
    assert client.get("/bins/B001").json()["inventory"][0]["name"] == "M4 T-nuts"
    example = InventoryStore(tmp_path / "inventory.db").examples_for_eval(limit=1)[0]
    assert example["kind"] == "confirmed"


def test_assistant_revision_replaces_pending_plan_and_can_be_confirmed(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "searchable_storage_led.controller.WledWriter.write_pixels",
        lambda self, pixels, config: None,
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                    {"size": "large", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(database_path),
    )
    store = InventoryStore(database_path)
    photo = store.add_photo(
        filename="t-nuts.jpg",
        content_type="image/jpeg",
        data=b"photo",
    )
    original_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="put away two M4 T-nuts",
        photo_ids=[photo.id],
        response={
            "message": "Use B001.",
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "M4 T-nuts",
                        "quantity": 2,
                        "location_id": "B001",
                    },
                }
            ],
        },
        status="pending",
    )

    def fake_run_agent(text, *args, **kwargs):
        assert "Original user request:\nput away two M4 T-nuts" in text
        assert '"location_id": "B001"' in text
        assert "User follow-up:\nuse B002 instead" in text
        assert kwargs["allowed_tools"] == VOICE_AGENT_TOOLS
        return SimpleNamespace(
            message="Updated proposal.",
            plan=[
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "M4 T-nuts",
                        "quantity": 2,
                        "location_id": "B002",
                    },
                }
            ],
            trace=[],
        )

    monkeypatch.setattr("searchable_storage_led.api.run_agent", fake_run_agent)
    client = TestClient(create_app(cfg))

    revised_response = client.post(
        "/assistant/revise",
        json={"text": "use B002 instead", "source": "voice"},
    )

    assert revised_response.status_code == 200
    revised = revised_response.json()
    assert revised["revision_of"] == original_id
    assert revised["source"] == "voice"
    assert revised["plan"][0]["arguments"]["location_id"] == "B002"
    assert revised["confirmation"]["schema"] == "magpie.confirmation.v1"
    assert store.assistant_interaction(original_id)["status"] == "superseded"
    revised_record = store.assistant_interaction(revised["interaction_id"])
    assert revised_record["route"] == "/assistant/revise"
    assert revised_record["status"] == "pending"
    assert revised_record["photo_ids"] == [photo.id]
    assert revised_record["command"] == (
        "put away two M4 T-nuts\nCorrection: use B002 instead"
    )

    confirmed = client.post("/assistant/voice/confirm").json()

    assert confirmed.get("source_interaction_id") == revised["interaction_id"], confirmed
    assert client.get("/bins/B001").json()["inventory"] == []
    assert client.get("/bins/B002").json()["inventory"][0]["name"] == "M4 T-nuts"
    correction_example = store.examples_for_eval(limit=1)[0]
    assert correction_example["kind"] == "correction"
    assert correction_example["command"] == (
        "put away two M4 T-nuts\nCorrection: use B002 instead"
    )
    correction_action = json.loads(correction_example["action"])
    assert correction_action[0]["arguments"]["location_id"] == "B002"
    correction_context = json.loads(correction_example["context_json"])
    assert correction_context["revision_of"] == original_id
    assert correction_context["correction"] == "use B002 instead"
    assert correction_context["original_plan"][0]["arguments"]["location_id"] == "B001"
    listed_correction = client.get(
        "/assistant/learning/examples?limit=1&kind=correction"
    ).json()["examples"][0]
    assert listed_correction["context"]["revision_of"] == original_id
    assert listed_correction["context"]["correction"] == "use B002 instead"
    assert listed_correction["context"]["original_plan"][0]["arguments"][
        "location_id"
    ] == "B001"

    future_examples = []

    def capture_future_examples(text, *args, **kwargs):
        future_examples.extend(kwargs["examples"])
        return SimpleNamespace(message="No change needed.", plan=[], trace=[])

    monkeypatch.setattr(
        "searchable_storage_led.api.run_agent",
        capture_future_examples,
    )
    future = client.post(
        "/assistant/chat",
        json={
            "text": "organize more M4 T-nuts",
            "mode": "agent",
            "source": "web",
        },
    )

    assert future.status_code == 200
    assert future_examples[0]["kind"] == "correction"
    assert json.loads(future_examples[0]["action"])[0]["arguments"][
        "location_id"
    ] == "B002"


def test_assistant_quick_size_revision_uses_storage_graph_without_model(
    tmp_path,
    monkeypatch,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                    {"size": "large", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    monkeypatch.setattr(
        "searchable_storage_led.api.run_agent",
        lambda *args, **kwargs: pytest.fail("quick revision called the model"),
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=2,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(database_path),
            )
        )
    )
    client.post(
        "/locations",
        json={
            "id": "small-parts",
            "type": "bin",
            "label": "Small Parts",
            "capacity_class": "small",
        },
    )
    client.post(
        "/locations",
        json={
            "id": "large-parts",
            "type": "bin",
            "label": "Large Parts",
            "capacity_class": "large",
        },
    )
    store = InventoryStore(database_path)
    original_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="put away M4 T-nuts",
        response={
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "M4 T-nuts",
                        "quantity": 2,
                        "location_id": "small-parts",
                    },
                }
            ],
        },
        status="pending",
    )

    revised = client.post(
        "/assistant/chat",
        json={
            "text": "use a larger bin",
            "source": "voice",
            "voice_transcript": "use a larger bin",
            "voice_command": "use a larger bin",
            "voice_query": "use a larger bin",
            "stt_model": "gpt-4o-mini-transcribe",
        },
    ).json()

    assert revised["revision_of"] == original_id
    assert revised["source"] == "voice"
    assert revised["usage"] == {}
    assert revised["plan"][0]["arguments"]["location_id"] == "large-parts"
    assert revised["trace"][0]["tool"] == "suggest_locations"
    assert revised["trace"][0]["arguments"]["change"] == "bigger"
    assert revised["active_finds"][0]["matches"][0]["location_id"] == (
        "large-parts"
    )
    assert revised["confirmation"]["change_prompts"][1]["id"] == (
        "bigger_container"
    )
    assert revised["voice"] == {
        "transcript": "use a larger bin",
        "command": "use a larger bin",
        "query": "use a larger bin",
        "stt_model": "gpt-4o-mini-transcribe",
    }
    assert store.assistant_interaction(original_id)["status"] == "superseded"

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "plan": revised["plan"],
            "command": "put away M4 T-nuts",
            "source_interaction_id": revised["interaction_id"],
        },
    ).json()

    assert confirmed["ok"] is True
    assert confirmed["applied"][0]["location_id"] == "large-parts"
    persisted = InventoryStore(database_path)
    stored_item = persisted.bin_inventory("large-parts")[0]
    assert stored_item.bin_id == "large-parts"
    assert stored_item.quantity == 2
    correction = persisted.examples_for_eval(limit=1, kind="correction")[0]
    assert json.loads(correction["context_json"])["correction"] == (
        "Use a bigger bin or container."
    )


def test_repeated_destination_revisions_keep_the_transaction_color(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {"name": "software-only", "columns": 1, "rows_bottom_to_top": []}
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    for location_id, capacity in (
        ("small-parts", "small"),
        ("large-parts", "large"),
        ("other-large-parts", "large"),
    ):
        response = client.post(
            "/locations",
            json={
                "id": location_id,
                "type": "bin",
                "label": location_id.replace("-", " ").title(),
                "capacity_class": capacity,
            },
        )
        assert response.status_code == 200

    original = client.post(
        "/assistant/chat",
        json={
            "text": "put away 2 M4 T-nuts",
            "source": "mobile",
        },
    ).json()
    original_decision = original["decision"]
    original_color = original_decision["active_finds"][0]["color"]
    assert original_decision["plan"][0]["arguments"]["location_id"] == (
        "small-parts"
    )

    larger = client.post(
        "/assistant/revise",
        json={
            "text": "Use a bigger bin or container.",
            "source": "mobile",
        },
    ).json()
    assert larger["original_command"] == "2 M4 T-nuts"
    assert larger["active_finds"][0]["color"] == original_color
    assert larger["active_finds"][0]["matches"][0]["location_id"] == (
        "large-parts"
    )

    selected = client.post(
        "/assistant/revise",
        json={
            "text": "Use Other Large Parts as the destination.",
            "source": "mobile",
            "location_id": "other-large-parts",
            "step_index": 0,
        },
    ).json()
    assert selected["revision_of"] == larger["interaction_id"]
    assert selected["original_command"] == "2 M4 T-nuts"
    assert selected["active_finds"][0]["color"] == original_color
    assert selected["active_finds"][0]["matches"][0]["location_id"] == (
        "other-large-parts"
    )
    assert len(client.get("/lights/active").json()["active_finds"]) == 1


def test_kiosk_can_apply_shared_quick_change_without_model(
    tmp_path,
    monkeypatch,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                    {"size": "large", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    monkeypatch.setattr(
        "searchable_storage_led.api.run_agent",
        lambda *args, **kwargs: pytest.fail("kiosk quick change called the model"),
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=2,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(database_path),
            )
        )
    )
    client.post(
        "/locations",
        json={
            "id": "small-parts",
            "type": "bin",
            "label": "Small Parts",
            "capacity_class": "small",
        },
    )
    client.post(
        "/locations",
        json={
            "id": "large-parts",
            "type": "bin",
            "label": "Large Parts",
            "capacity_class": "large",
        },
    )
    store = InventoryStore(database_path)
    original_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="put away M4 T-nuts",
        response={
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "M4 T-nuts",
                        "quantity": 2,
                        "location_id": "small-parts",
                    },
                }
            ],
        },
        status="pending",
    )

    invalid = client.post(
        "/kiosk/actions/revise",
        json={"prompt_id": "invent_a_place"},
    )
    revised_response = client.post(
        "/kiosk/actions/revise",
        json={"prompt_id": "bigger_container"},
    )

    assert invalid.status_code == 400
    assert invalid.json()["detail"] == (
        "that change is not available for this proposal"
    )
    assert revised_response.status_code == 200
    revised = revised_response.json()
    assert revised["revision_of"] == original_id
    assert revised["source"] == "kiosk"
    assert revised["change_prompt_id"] == "bigger_container"
    assert revised["plan"][0]["arguments"]["location_id"] == "large-parts"
    assert revised["confirmation"]["schema"] == "magpie.confirmation.v1"
    assert store.assistant_interaction(original_id)["status"] == "superseded"
    assert store.assistant_interaction(revised["interaction_id"])["status"] == (
        "pending"
    )


def test_assistant_selected_destination_revision_is_explicit_and_retained(
    tmp_path,
    monkeypatch,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    monkeypatch.setattr(
        "searchable_storage_led.api.run_agent",
        lambda *args, **kwargs: pytest.fail(
            "selected destination revision called the model"
        ),
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(database_path),
            )
        )
    )
    for location_id, label in (
        ("original-bin", "Original Bin"),
        ("chosen-bin", "Chosen Bin"),
    ):
        response = client.post(
            "/locations",
            json={
                "id": location_id,
                "type": "bin",
                "label": label,
                "accepts_items": True,
            },
        )
        assert response.status_code == 200
    rejected = client.post(
        "/locations",
        json={
            "id": "display-only",
            "type": "cabinet",
            "label": "Display Only",
            "accepts_items": False,
        },
    )
    assert rejected.status_code == 200

    store = InventoryStore(database_path)
    original_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="put away M3 screws",
        response={
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "M3 screws",
                        "quantity": 12,
                        "location_id": "original-bin",
                    },
                }
            ],
        },
        status="pending",
    )

    clarification = client.post(
        "/assistant/revise",
        json={"text": "Use a different storage place.", "source": "voice"},
    ).json()
    assert clarification["plan"] == []
    assert clarification["action"] == "clarify"
    assert clarification["original_plan_still_pending"] is True
    assert clarification["message"].startswith("Choose the storage place")
    assert store.assistant_interaction(original_id)["status"] == "pending"

    invalid = client.post(
        "/assistant/revise",
        json={
            "text": "Use Display Only as the destination.",
            "location_id": "display-only",
            "step_index": 0,
        },
    )
    assert invalid.status_code == 400
    assert "accepts items" in invalid.json()["detail"]

    revised = client.post(
        "/assistant/revise",
        json={
            "text": "Use Chosen Bin as the destination.",
            "source": "web",
            "location_id": "chosen-bin",
            "step_index": 0,
        },
    ).json()

    assert revised["revision_of"] == original_id
    assert revised["source"] == "web"
    assert revised["usage"] == {}
    assert revised["plan"][0]["arguments"]["location_id"] == "chosen-bin"
    assert revised["trace"][0]["tool"] == "select_location"
    assert revised["trace"][0]["arguments"] == {
        "step_index": 0,
        "location_id": "chosen-bin",
    }
    assert revised["confirmation"]["rows"][0]["destination"]["id"] == "chosen-bin"
    assert store.assistant_interaction(original_id)["status"] == "superseded"

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "plan": revised["plan"],
            "command": "put away M3 screws",
            "source_interaction_id": revised["interaction_id"],
        },
    ).json()

    assert confirmed["ok"] is True
    assert confirmed["applied"][0]["location_id"] == "chosen-bin"
    correction = InventoryStore(database_path).examples_for_eval(
        limit=1,
        kind="correction",
    )[0]
    assert json.loads(correction["context_json"])["correction"] == (
        "Use Chosen Bin as the destination."
    )


def test_assistant_revision_without_a_new_plan_keeps_original_pending(
    tmp_path,
    monkeypatch,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="none",
        layout_path=str(layout_path),
        database_path=str(database_path),
    )
    store = InventoryStore(database_path)
    original_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="put away fuses",
        response={
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "fuses",
                        "quantity": 1,
                        "location_id": "B001",
                    },
                }
            ],
        },
        status="pending",
    )
    monkeypatch.setattr(
        "searchable_storage_led.api.run_agent",
        lambda *args, **kwargs: SimpleNamespace(
            message="B001 is already the smallest available bin.",
            plan=[],
            trace=[],
        ),
    )
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/revise",
        json={"text": "can it use a smaller bin?", "source": "mobile"},
    )

    assert response.status_code == 200
    assert response.json()["original_plan_still_pending"] is True
    assert store.assistant_interaction(original_id)["status"] == "pending"
    pending = client.get("/kiosk/state?wait_seconds=0").json()[
        "pending_confirmation"
    ]
    assert pending["id"] == original_id


def test_assistant_revision_requires_a_pending_plan(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )

    response = client.post(
        "/assistant/revise",
        json={"text": "use a larger bin"},
    )

    assert response.status_code == 409
    assert "no pending Magpie proposal" in response.json()["detail"]


def test_assistant_chat_routes_rule_questions_to_agent_with_trace(tmp_path, monkeypatch) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )

    def fake_run_agent(text, *args, **kwargs):
        assert text == "what rules do you know about jewelry?"
        return SimpleNamespace(
            message="You have one jewelry rule.",
            plan=[],
            trace=[
                {
                    "tool": "list_rules",
                    "arguments": {"query": "jewelry"},
                    "result": {
                        "placement_rules": [
                            {
                                "rule_type": "placement",
                                "id": 7,
                                "rule_text": "Jewelry goes in the main cabinet.",
                                "trigger_terms": ["jewelry"],
                                "target_location_id": "main-cabinet",
                                "source_command": "I always keep jewelry in the main cabinet",
                                "confidence": 1.0,
                                "active": True,
                            }
                        ],
                        "size_rules": [],
                    },
                }
            ],
        )

    monkeypatch.setattr("searchable_storage_led.api.run_agent", fake_run_agent)
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/chat",
        json={"text": "what rules do you know about jewelry?"},
    ).json()

    assert response["mode"] == "agent"
    assert response["plan"] == []
    assert response["trace"][0]["tool"] == "list_rules"
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert latest["route"] == "/assistant/chat"


def test_assistant_chat_previews_label_sheet_without_model(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_AI_API_TOKEN", raising=False)
    monkeypatch.setattr(
        "searchable_storage_led.api.run_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("label previews should not call the planning model")
        ),
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    for command in (
        "labels for main cabinet",
        "Print labels for main cabinet",
        "Make QR labels for main cabinet",
    ):
        response = client.post(
            "/assistant/chat",
            json={"text": command},
        ).json()

        assert response["mode"] == "agent"
        assert response["plan"] == []
        assert response["trace"][0]["tool"] == "preview_label_sheet"
        assert response["trace"][0]["arguments"] == {"location_id": "main-cabinet"}
        assert response["label_preview"]["found"] is True
        assert response["label_preview"]["url"] == "/locations/main-cabinet/labels.html"
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert latest["route"] == "/assistant/chat"


def test_assistant_chat_routes_alias_requests_to_agent(tmp_path, monkeypatch) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )

    def fake_run_agent(text, *args, **kwargs):
        assert text == "I call M4 T-nuts tee nuts"
        return SimpleNamespace(
            message="I can learn that alias.",
            plan=[
                {
                    "tool": "learn_alias",
                    "arguments": {"item": "M4 T-nuts", "alias": "tee nuts"},
                }
            ],
            trace=[],
        )

    monkeypatch.setattr("searchable_storage_led.api.run_agent", fake_run_agent)
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/chat",
        json={"text": "I call M4 T-nuts tee nuts"},
    ).json()

    assert response["mode"] == "agent"
    assert response["plan"][0]["tool"] == "learn_alias"
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert latest["route"] == "/assistant/chat"
    assert latest["status"] == "pending"


def test_assistant_chat_routes_item_metadata_updates_to_agent(tmp_path, monkeypatch) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )

    def fake_run_agent(text, *args, **kwargs):
        assert text == "rename Generic Hardware to M4 T-nuts"
        return SimpleNamespace(
            message="I can rename that.",
            plan=[
                {
                    "tool": "update_item",
                    "arguments": {"item": "Generic Hardware", "name": "M4 T-nuts"},
                }
            ],
            trace=[],
        )

    monkeypatch.setattr("searchable_storage_led.api.run_agent", fake_run_agent)
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/chat",
        json={"text": "rename Generic Hardware to M4 T-nuts"},
    ).json()

    assert response["mode"] == "agent"
    assert response["plan"][0]["tool"] == "update_item"
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert latest["route"] == "/assistant/chat"
    assert latest["status"] == "pending"


def test_assistant_chat_item_home_update_can_confirm_and_undo(tmp_path, monkeypatch) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 3,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 3, "spans": [1, 1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=3,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="M4 T-nuts")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=2, unit="each")
    store.move_inventory(item_id=item_id, from_bin="B001", to_bin="B002")

    def fake_run_agent(text, *args, **kwargs):
        assert text == "M4 T-nuts should live in B003"
        return SimpleNamespace(
            message="I can make B003 the home for M4 T-nuts.",
            plan=[
                {
                    "tool": "update_item_home",
                    "arguments": {"item_id": item_id, "location_id": "B003"},
                }
            ],
            trace=[
                {
                    "tool": "search_items",
                    "arguments": {"query": "M4 T-nuts"},
                    "result": {"results": [store.search("M4 T-nuts")[0].as_dict()]},
                }
            ],
        )

    monkeypatch.setattr("searchable_storage_led.api.run_agent", fake_run_agent)
    client = TestClient(create_app(cfg))

    proposed = client.post(
        "/assistant/chat",
        json={"text": "M4 T-nuts should live in B003"},
    ).json()

    assert proposed["mode"] == "agent"
    assert proposed["route"] == "/assistant/chat"
    assert proposed["plan"][0]["tool"] == "update_item_home"
    assert proposed["interaction_id"] > 0
    pending = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert pending["route"] == "/assistant/chat"
    assert pending["status"] == "pending"

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "M4 T-nuts should live in B003",
            "source_interaction_id": proposed["interaction_id"],
            "plan": proposed["plan"],
        },
    ).json()

    assert confirmed["ok"] is True
    assert confirmed["source_interaction_id"] == proposed["interaction_id"]
    assert confirmed["applied"][0]["updated_item_home"] == item_id
    assert confirmed["applied"][0]["previous_home_location_id"] == "B001"
    visible = client.get("/bins/B002").json()["inventory"][0]
    assert visible["home_location_id"] == "B003"
    assert visible["last_seen_location_id"] == "B002"

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    assert undone["source_interaction_id"] != proposed["interaction_id"]
    assert undone["undone"][0]["restored_item_home"] == item_id
    restored = client.get("/bins/B002").json()["inventory"][0]
    assert restored["home_location_id"] == "B001"
    assert restored["last_seen_location_id"] == "B002"


def test_assistant_chat_bulk_reorganization_can_confirm_and_undo(
    tmp_path, monkeypatch
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    allen_id = store.upsert_item(name="Allen keys")
    wrench_id = store.upsert_item(name="metric wrenches")
    store.put_inventory(item_id=allen_id, bin_id="B001", quantity=1, unit="set")
    store.put_inventory(item_id=wrench_id, bin_id="B002", quantity=1, unit="set")

    def fake_run_agent(text, *args, **kwargs):
        assert text == "move all Allen keys and wrenches to a big bin"
        plan = [
            {
                "tool": "create_location",
                "arguments": {
                    "type": "bin",
                    "label": "Large Hand Tool Bin",
                    "parent_id": "main-cabinet",
                    "capacity_class": "large",
                    "purpose_tags": ["hand-tools"],
                },
            },
            {
                "tool": "move_item",
                "arguments": {
                    "item_id": allen_id,
                    "item": "Allen keys",
                    "from_location_id": "B001",
                    "to_location_id": "large-hand-tool-bin",
                },
            },
            {
                "tool": "move_item",
                "arguments": {
                    "item_id": wrench_id,
                    "item": "metric wrenches",
                    "from_location_id": "B002",
                    "to_location_id": "large-hand-tool-bin",
                },
            },
        ]
        return SimpleNamespace(
            message="I found two tool groups to consolidate.",
            plan=plan,
            trace=[
                {
                    "tool": "plan_reorganization",
                    "arguments": {
                        "queries": ["Allen keys", "wrenches"],
                        "destination_label": "Large Hand Tool Bin",
                    },
                    "result": {
                        "destination_id": "large-hand-tool-bin",
                        "move_count": 2,
                        "proposed_steps": plan,
                    },
                }
            ],
        )

    monkeypatch.setattr("searchable_storage_led.api.run_agent", fake_run_agent)
    client = TestClient(create_app(cfg))

    proposed = client.post(
        "/assistant/chat",
        json={"text": "move all Allen keys and wrenches to a big bin"},
    ).json()

    assert proposed["mode"] == "agent"
    assert [step["tool"] for step in proposed["plan"]] == [
        "create_location",
        "move_item",
        "move_item",
    ]
    assert proposed["trace"][0]["tool"] == "plan_reorganization"
    pending = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert pending["route"] == "/assistant/chat"
    assert pending["status"] == "pending"

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "move all Allen keys and wrenches to a big bin",
            "source_interaction_id": proposed["interaction_id"],
            "plan": proposed["plan"],
        },
    ).json()

    assert confirmed["ok"] is True
    assert confirmed["source_interaction_id"] == proposed["interaction_id"]
    assert [entry.get("moved_item") for entry in confirmed["applied"] if entry.get("moved_item")] == [
        allen_id,
        wrench_id,
    ]
    destination = client.get("/locations/large-hand-tool-bin").json()
    assert {item["item_id"] for item in destination["items"]} == {allen_id, wrench_id}

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    assert any(entry.get("moved_item") == allen_id for entry in undone["undone"])
    assert any(entry.get("moved_item") == wrench_id for entry in undone["undone"])
    assert any(entry.get("deleted_location") == "large-hand-tool-bin" for entry in undone["undone"])
    assert client.get("/locations/B001").json()["items"][0]["item_id"] == allen_id
    assert client.get("/locations/B002").json()["items"][0]["item_id"] == wrench_id
    assert client.get("/locations/large-hand-tool-bin").status_code == 404


def test_assistant_chat_routes_remove_item_requests_to_agent(tmp_path, monkeypatch) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )

    def fake_run_agent(text, *args, **kwargs):
        assert text == "remove dead batteries"
        return SimpleNamespace(
            message="I can remove those.",
            plan=[
                {
                    "tool": "remove_item",
                    "arguments": {"item": "dead batteries", "location_id": "B001"},
                }
            ],
            trace=[],
        )

    monkeypatch.setattr("searchable_storage_led.api.run_agent", fake_run_agent)
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/chat",
        json={"text": "remove dead batteries"},
    ).json()

    assert response["mode"] == "agent"
    assert response["plan"][0]["tool"] == "remove_item"
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert latest["route"] == "/assistant/chat"
    assert latest["status"] == "pending"


def test_confirmed_agent_plan_learns_alias_for_existing_item(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="M4 T-nuts")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=2, unit="each")
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/confirm",
        json={
            "command": "I call M4 T-nuts tee nuts",
            "plan": [
                {
                    "tool": "learn_alias",
                    "arguments": {"item_id": item_id, "alias": "tee nuts"},
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"][0]["learned_alias"] == "tee nuts"
    assert body["applied"][0]["item_id"] == item_id
    assert store.search("tee nuts")[0].name == "M4 T-nuts"
    latest_example = store.examples_for_eval(limit=1)[0]
    assert latest_example["command"] == "I call M4 T-nuts tee nuts"


def test_confirmed_agent_plan_removes_existing_item(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="dead batteries")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=4, unit="each")
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/confirm",
        json={
            "command": "remove dead batteries",
            "plan": [
                {
                    "tool": "remove_item",
                    "arguments": {"item_id": item_id, "location_id": "B001"},
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"][0]["removed_item"] == item_id
    assert body["applied"][0]["quantity"] == 4
    assert store.items_in_location("B001") == []


def test_voice_confirm_applies_latest_pending_agent_plan(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(db_path)
    interaction_id = store.record_assistant_interaction(
        route="/assistant/agent",
        command="put spare fuses in B001",
        response={
            "ok": True,
            "message": "I can file those.",
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "spare fuses",
                        "location_id": "B001",
                        "quantity": 10,
                    },
                }
            ],
        },
        status="pending",
    )

    response = client.post("/assistant/voice/confirm")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["source_interaction_id"] == interaction_id
    assert body["applied"][0]["stored"] == "spare fuses"
    assert client.get("/bins/B001").json()["inventory"][0]["name"] == "spare fuses"
    latest = client.get("/assistant/interactions?limit=10").json()["interactions"]
    pending = [row for row in latest if row["id"] == interaction_id][0]
    assert pending["status"] == "confirmed"


def test_stale_pending_plan_expires_and_cannot_be_voice_confirmed(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="none",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(db_path)
    interaction_id = store.record_assistant_interaction(
        route="/assistant/agent",
        command="put stale fuses in B001",
        response={
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "stale fuses",
                        "location_id": "B001",
                        "quantity": 10,
                    },
                }
            ],
        },
        status="pending",
    )
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE assistant_interactions
            SET created_at = datetime('now', '-10 minutes')
            WHERE id = ?
            """,
            (interaction_id,),
        )

    interactions = client.get("/assistant/interactions?limit=10").json()["interactions"]
    expired = next(row for row in interactions if row["id"] == interaction_id)
    assert expired["status"] == "expired"
    assert expired["confirmable"] is False
    assert expired["age_seconds"] >= 600
    kiosk_state = client.get(
        "/kiosk/state",
        params={"after_id": interaction_id, "wait_seconds": 0},
    ).json()
    assert kiosk_state["interactions"] == []
    assert kiosk_state["pending_confirmation"] is None

    confirmed = client.post("/assistant/voice/confirm")

    assert confirmed.status_code == 200
    assert confirmed.json()["ok"] is False
    assert client.get("/bins/B001").json()["inventory"] == []
    assert store.assistant_interaction(interaction_id)["status"] == "expired"


def test_stale_putaway_proposal_removes_its_active_find(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "searchable_storage_led.controller.WledWriter.write_pixels",
        lambda self, pixels, config: None,
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="wled",
                wled_url="http://127.0.0.1:1",
                layout_path=str(layout_path),
                database_path=str(db_path),
            )
        )
    )
    proposed = client.post(
        "/assistant/chat",
        json={"text": "put away stale fuses"},
    ).json()
    interaction_id = proposed["decision"]["interaction_id"]

    assert client.get("/lights/active").json()["active_finds"]

    store = InventoryStore(db_path)
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE assistant_interactions
            SET created_at = datetime('now', '-10 minutes')
            WHERE id = ?
            """,
            (interaction_id,),
        )

    interactions = client.get("/assistant/interactions?limit=10").json()["interactions"]
    expired = next(row for row in interactions if row["id"] == interaction_id)

    assert expired["status"] == "expired"
    assert expired["active_find_removed"] is True
    assert expired["active_find_light_ok"] is True
    assert expired["active_find_light_errors"] == []
    assert client.get("/lights/active").json()["active_finds"] == []


def test_voice_confirm_uses_new_putaway_instead_of_stale_pending_plan(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(db_path)
    stale_id = store.record_assistant_interaction(
        route="/assistant/agent",
        command="move old item",
        response={
            "plan": [
                {
                    "tool": "move_item",
                    "arguments": {
                        "item_id": 72,
                        "from_location_id": "B041",
                        "to_location_id": "B001",
                    },
                }
            ]
        },
        status="pending",
    )

    proposal = client.post(
        "/assistant/chat",
        json={
            "text": "put away laser cutter key into a small bin in the main cabinet",
            "source": "voice",
        },
    ).json()
    latest = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert proposal["decision"]["placement"]["reason"] == (
        "you asked for a small bin in the main cabinet"
    )
    assert latest["id"] != stale_id
    assert latest["status"] == "pending"

    confirmed = client.post("/assistant/voice/confirm")

    assert confirmed.status_code == 200
    assert confirmed.json()["source_interaction_id"] == latest["id"]
    assert client.get("/bins/B001").json()["inventory"][0]["name"] == (
        proposal["decision"]["item"]["name"]
    )


def test_voice_cancel_marks_latest_pending_plan_without_applying(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(db_path)
    interaction_id = store.record_assistant_interaction(
        route="/assistant/agent",
        command="put spare fuses in B001",
        response={
            "ok": True,
            "message": "I can file those.",
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "spare fuses",
                        "location_id": "B001",
                        "quantity": 10,
                    },
                }
            ],
        },
        status="pending",
    )

    response = client.post("/assistant/voice/cancel")

    assert response.status_code == 200
    assert response.json()["source_interaction_id"] == interaction_id
    assert client.get("/bins/B001").json()["inventory"] == []
    latest = client.get("/assistant/interactions?limit=10").json()["interactions"]
    pending = [row for row in latest if row["id"] == interaction_id][0]
    assert pending["status"] == "canceled"


def test_assistant_undo_reverses_latest_confirmed_put_item_plan(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "put spare fuses in B001",
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "spare fuses",
                        "location_id": "B001",
                        "quantity": 10,
                    },
                }
            ],
        },
    ).json()
    assert confirmed["applied"][0]["stored"] == "spare fuses"

    undone = client.post("/assistant/undo")

    assert undone.status_code == 200
    body = undone.json()
    assert body["ok"] is True
    assert body["undo_interaction_id"] > 0
    assert body["undone"][0]["removed_item"] == confirmed["applied"][0]["item_id"]
    assert client.get("/bins/B001").json()["inventory"] == []
    second = client.post("/assistant/undo").json()
    assert second["ok"] is False
    assert second["undo_interaction_id"] > body["undo_interaction_id"]


def test_assistant_undo_restores_latest_confirmed_remove_item_plan(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="dead batteries")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=4, unit="each")
    client = TestClient(create_app(cfg))

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "remove dead batteries",
            "plan": [
                {
                    "tool": "remove_item",
                    "arguments": {"item_id": item_id, "location_id": "B001"},
                }
            ],
        },
    ).json()
    assert confirmed["applied"][0]["removed_item"] == item_id
    assert client.get("/bins/B001").json()["inventory"] == []

    undone = client.post("/assistant/undo")

    assert undone.status_code == 200
    body = undone.json()
    assert body["ok"] is True
    assert body["undone"][0]["restored_item"] == item_id
    assert body["undone"][0]["quantity"] == 4
    restored = client.get("/bins/B001").json()["inventory"][0]
    assert restored["item_id"] == item_id
    assert restored["quantity"] == 4
    assert restored["unit"] == "each"
    second = client.post("/assistant/undo").json()
    assert second["ok"] is False


def test_assistant_can_confirm_and_undo_review_approval(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    created = client.post(
        "/assistant/confirm",
        json={
            "command": "store uncertain sensor board",
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "uncertain sensor board",
                        "location_id": "B001",
                        "needs_review": True,
                    },
                }
            ],
        },
    ).json()
    item_id = created["applied"][0]["item_id"]

    approved = client.post(
        "/assistant/confirm",
        json={
            "command": "approve the sensor board",
            "plan": [
                {
                    "tool": "approve_review_item",
                    "arguments": {"item_id": item_id, "location_id": "B001"},
                }
            ],
        },
    ).json()

    assert approved["applied"][0]["approved_review"] == item_id
    assert client.get("/inventory/review").json()["items"] == []

    undone = client.post("/assistant/undo").json()

    assert undone["undone"][0]["restored_review"] == item_id
    review_items = client.get("/inventory/review").json()["items"]
    assert review_items[0]["item_id"] == item_id


def test_assistant_undo_restores_latest_confirmed_update_item_plan(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="Generic Hardware", category="unknown", aliases=["misc fastener"])
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=2, unit="each")
    client = TestClient(create_app(cfg))

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "rename Generic Hardware to M4 T-nuts and mark it as fasteners",
            "plan": [
                {
                    "tool": "update_item",
                    "arguments": {
                        "item_id": item_id,
                        "name": "M4 T-nuts",
                        "category": "fasteners",
                    },
                }
            ],
        },
    ).json()
    assert confirmed["applied"][0]["updated_item"] == item_id
    assert confirmed["applied"][0]["previous_category"] == "unknown"
    assert store.items_in_location("B001")[0].name == "M4 T-nuts"

    undone = client.post("/assistant/undo")

    assert undone.status_code == 200
    body = undone.json()
    assert body["ok"] is True
    assert body["undone"][0]["restored_item_metadata"] == item_id
    restored = store.items_in_location("B001")[0]
    assert restored.name == "Generic Hardware"
    assert restored.category == "unknown"
    assert restored.aliases == ["misc fastener"]
    assert store.search("M4 T-nuts") == []
    assert store.search("Generic Hardware")[0].item_id == item_id


def test_assistant_undo_restores_latest_confirmed_update_item_size_plan(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="Jewelry Box")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")
    store.update_item_size(item_id=item_id, size_class="small", source="manual", confidence=0.7)
    client = TestClient(create_app(cfg))

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "Jewelry Box is medium",
            "plan": [
                {
                    "tool": "update_item_size",
                    "arguments": {"item": "Jewelry Box", "size_class": "medium"},
                }
            ],
        },
    ).json()
    assert confirmed["applied"][0]["previous_size_class"] == "small"
    assert store.item_size_class("Jewelry Box") == "medium"

    undone = client.post("/assistant/undo")

    assert undone.status_code == 200
    body = undone.json()
    assert body["ok"] is True
    assert body["undone"][0]["restored_item_size"] == item_id
    assert body["undone"][0]["size_class"] == "small"
    restored = store.items_in_location("B001")[0]
    assert restored.size_class == "small"
    assert restored.size_source == "manual"
    assert restored.size_confidence == 0.7


def test_confirmed_putaways_adapt_item_home_and_undo_rejects_learning(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="USB adapters")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")
    client = TestClient(create_app(cfg))
    renamed = client.post(
        "/locations/B002/update",
        json={"label": "Adapter Drawer"},
    )
    assert renamed.status_code == 200
    request = {
        "command": "Put another USB adapter in B002",
        "plan": [
            {
                "tool": "put_item",
                "arguments": {
                    "item": "USB adapters",
                    "location_id": "B002",
                    "quantity": 1,
                    "unit": "each",
                    "quantity_mode": "add",
                },
            }
        ],
    }

    first = client.post("/assistant/confirm", json=request).json()
    second = client.post("/assistant/confirm", json=request).json()

    assert first["home_learning"][0]["promoted"] is False
    learned = second["home_learning"][0]
    assert learned["promoted"] is True
    assert learned["previous_home_location_id"] == "B001"
    assert second["learning_message"] == (
        "I learned the usual home for USB adapters: Test › Adapter Drawer."
    )
    assert store.item_inventory(item_id)[0].home_location_id == "B002"

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    assert undone["undone"][-1] == {
        "home_observation": learned["home_observation"],
        "item_id": item_id,
        "restored_home_location_id": "B001",
    }
    assert store.item_inventory(item_id)[0].home_location_id == "B001"
    locked = store.observe_confirmed_home(item_id=item_id, location_id="B002")
    assert locked["explicit_home"] is True
    assert locked["promoted"] is False


def test_assistant_undo_restores_latest_confirmed_update_item_home_plan(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 3,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 3, "spans": [1, 1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=3,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="M4 T-nuts")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=2, unit="each")
    store.move_inventory(item_id=item_id, from_bin="B001", to_bin="B002")
    client = TestClient(create_app(cfg))

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "M4 T-nuts should live in B003",
            "plan": [
                {
                    "tool": "update_item_home",
                    "arguments": {"item": "M4 T-nuts", "location_id": "B003"},
                }
            ],
        },
    ).json()
    assert confirmed["applied"][0]["previous_home_location_id"] == "B001"
    assert store.search("M4 T-nuts")[0].home_location_id == "B003"

    undone = client.post("/assistant/undo")

    assert undone.status_code == 200
    body = undone.json()
    assert body["ok"] is True
    assert body["undone"][0]["restored_item_home"] == item_id
    assert body["undone"][0]["home_location_id"] == "B001"
    restored = store.search("M4 T-nuts")[0]
    assert restored.home_location_id == "B001"
    assert restored.last_seen_location_id == "B002"


def test_assistant_undo_restores_item_size_changed_during_putaway(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="Camera case")
    store.update_item_size(
        item_id=item_id,
        size_class="small",
        source="user",
        confidence=0.8,
        dimensions={"length_mm": 100, "width_mm": 80, "height_mm": 30},
    )
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")
    client = TestClient(create_app(cfg))

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "Put away another 12 by 8 by 3 inch camera case",
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "Camera case",
                        "location_id": "B001",
                        "quantity_mode": "add",
                        "size_class": "large",
                        "dimensions": {
                            "length_mm": 304.8,
                            "width_mm": 203.2,
                            "height_mm": 76.2,
                        },
                    },
                }
            ],
        },
    ).json()

    assert confirmed["applied"][1]["updated_item_size"] == item_id
    changed = store.exact_match("Camera case")[0]
    assert changed.quantity == 2
    assert changed.size_class == "large"
    assert changed.dimensions["length_mm"] == 304.8

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    restored = store.exact_match("Camera case")[0]
    assert restored.quantity == 1
    assert restored.size_class == "small"
    assert restored.size_source == "user"
    assert restored.size_confidence == 0.8
    assert restored.dimensions == {
        "length_mm": 100,
        "width_mm": 80,
        "height_mm": 30,
    }


def test_assistant_undo_restores_latest_confirmed_update_location_plan(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    locations = LocationStore(db_path)
    locations.upsert(
        Location(
            id="solder-pouch",
            type="pouch",
            label="Solder Pouch",
            parent_id="B001",
            capacity_class="small",
            purpose_tags=["solder"],
            placement_notes="Solder wick only.",
            internal_dimensions={"width_mm": 100, "depth_mm": 80, "height_mm": 30},
        )
    )

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "rename Solder Pouch to Solder Kit and mark it medium",
            "plan": [
                {
                    "tool": "update_location",
                    "arguments": {
                        "location_id": "Solder Pouch",
                        "label": "Solder Kit",
                        "capacity_class": "medium",
                        "purpose_tags": ["solder", "electronics"],
                        "placement_notes": "Solder wick, flux, tips, and small soldering tools.",
                        "internal_dimensions": {
                            "width_mm": 200,
                            "depth_mm": 160,
                            "height_mm": 60,
                        },
                    },
                }
            ],
        },
    ).json()
    assert confirmed["applied"][0]["updated_location"] == "solder-pouch"
    updated = locations.get("solder-pouch")
    assert updated.label == "Solder Kit"
    assert updated.capacity_class == "medium"
    assert updated.purpose_tags == ["solder", "electronics"]
    assert updated.internal_dimensions == {
        "width_mm": 200.0,
        "depth_mm": 160.0,
        "height_mm": 60.0,
    }

    undone = client.post("/assistant/undo")

    assert undone.status_code == 200
    body = undone.json()
    assert body["ok"] is True
    assert body["undone"][0]["restored_location"] == "solder-pouch"
    restored = locations.get("solder-pouch")
    assert restored.label == "Solder Pouch"
    assert restored.type == "pouch"
    assert restored.capacity_class == "small"
    assert restored.purpose_tags == ["solder"]
    assert restored.placement_notes == "Solder wick only."
    assert restored.internal_dimensions == {
        "width_mm": 100,
        "depth_mm": 80,
        "height_mm": 30,
    }


def test_assistant_undo_restores_previous_item_or_location_photo_target(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    photo = store.add_photo(filename="before.jpg", content_type="image/jpeg", data=b"photo")
    item_id = store.upsert_item(name="solder wick")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="roll")
    store.set_photo_target(photo_id=photo.id, location_id="B001")
    client = TestClient(create_app(cfg))

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "attach photo to solder wick",
            "plan": [
                {
                    "tool": "attach_photo",
                    "arguments": {
                        "photo_id": photo.id,
                        "object_type": "item",
                        "object_id": item_id,
                    },
                }
            ],
        },
    ).json()
    assert confirmed["applied"][0]["attached_photo"] == photo.id
    assert confirmed["applied"][0]["previous_location_id"] == "B001"
    attached = store.photo(photo.id)
    assert attached.item_id == item_id
    assert attached.location_id is None

    undone = client.post("/assistant/undo")

    assert undone.status_code == 200
    body = undone.json()
    assert body["ok"] is True
    assert body["undone"][0]["restored_photo_target"] == photo.id
    restored = store.photo(photo.id)
    assert restored.item_id is None
    assert restored.location_id == "B001"

    store.set_photo_target(photo_id=photo.id, item_id=item_id)
    confirmed_location = client.post(
        "/assistant/confirm",
        json={
            "command": "attach photo to the drawer",
            "plan": [
                {
                    "tool": "attach_photo",
                    "arguments": {
                        "photo_id": photo.id,
                        "object_type": "location",
                        "object_id": "B001",
                    },
                }
            ],
        },
    ).json()
    assert confirmed_location["applied"][0]["previous_item_id"] == item_id
    assert store.photo(photo.id).location_id == "B001"

    undone_location = client.post("/assistant/undo")

    assert undone_location.status_code == 200
    restored_item = store.photo(photo.id)
    assert restored_item.item_id == item_id
    assert restored_item.location_id is None


def test_assistant_undo_clears_item_size_when_previous_size_was_unset(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="Jewelry Box")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")
    client = TestClient(create_app(cfg))

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "Jewelry Box is medium",
            "plan": [
                {
                    "tool": "update_item_size",
                    "arguments": {"item": "Jewelry Box", "size_class": "medium"},
                }
            ],
        },
    ).json()
    assert confirmed["applied"][0]["previous_size_class"] == ""
    assert store.item_size_class("Jewelry Box") == "medium"

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    assert undone["undone"][0]["restored_item_size"] == item_id
    assert undone["undone"][0]["size_class"] == ""
    assert store.item_size_class("Jewelry Box") is None
    assert store.items_in_location("B001")[0].size_class == ""


def test_assistant_undo_removes_latest_confirmed_learn_alias_plan(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="M4 T-nuts")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=2, unit="each")
    client = TestClient(create_app(cfg))

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "I call M4 T-nuts tee nuts",
            "plan": [
                {
                    "tool": "learn_alias",
                    "arguments": {"item_id": item_id, "alias": "tee nuts"},
                }
            ],
        },
    ).json()
    assert confirmed["applied"][0]["learned_alias"] == "tee nuts"
    assert store.search("tee nuts")[0].item_id == item_id

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    assert undone["undone"][0]["removed_alias"] == "tee nuts"
    assert store.search("tee nuts") == []


def test_assistant_undo_forgets_latest_confirmed_learn_rule_plan(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "I always keep jewelry in the main cabinet",
            "plan": [
                {
                    "tool": "learn_rule",
                    "arguments": {
                        "rule_text": "Jewelry should go in the main cabinet.",
                        "trigger_terms": ["jewelry"],
                        "target_location_id": "main-cabinet",
                    },
                }
            ],
        },
    ).json()
    rule_id = confirmed["applied"][0]["learned_rule"]
    example = client.get(
        "/assistant/learning/examples?limit=1&kind=confirmed"
    ).json()["examples"][0]
    assert example["review_status"] == "promoted"
    assert example["promotable"] is False
    assert example["forgettable"] is False

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    assert undone["undone"][0] == {"forgotten_rule": rule_id, "rule_type": "placement"}
    assert InventoryStore(cfg.database_path).matching_rules("jewelry") == []


def test_assistant_undo_forgets_latest_confirmed_learn_size_rule_plan(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "Jewelry boxes are medium",
            "plan": [
                {
                    "tool": "learn_size_rule",
                    "arguments": {
                        "rule_text": "Jewelry boxes are medium.",
                        "trigger_terms": ["jewelry", "box"],
                        "size_class": "medium",
                    },
                }
            ],
        },
    ).json()
    rule_id = confirmed["applied"][0]["learned_size_rule"]

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    assert undone["undone"][0] == {"forgotten_rule": rule_id, "rule_type": "size"}
    assert InventoryStore(cfg.database_path).matching_size_rules("jewelry box") == []


def test_assistant_undo_restores_latest_confirmed_update_rule_plan(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(cfg.database_path)
    rule_id = store.learn_rule(
        rule_text="Jewelry should go in B001.",
        trigger_terms=["jewelry"],
        target_location_id="B001",
        source_command="old",
        confidence=0.4,
    )

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "Actually jewelry goes in B002",
            "plan": [
                {
                    "tool": "update_rule",
                    "arguments": {
                        "rule_id": rule_id,
                        "rule_text": "Jewelry should go in B002.",
                        "trigger_terms": ["jewelry", "rings"],
                        "target_location_id": "B002",
                        "source_command": "new",
                        "confidence": 0.9,
                    },
                }
            ],
        },
    ).json()

    assert confirmed["applied"][0]["previous_target_location_id"] == "B001"
    assert InventoryStore(cfg.database_path).matching_rules("rings")[0].target_location_id == "B002"

    undone = client.post("/assistant/undo").json()
    restored = InventoryStore(cfg.database_path).rule(rule_id)

    assert undone["ok"] is True
    assert undone["undone"][0] == {"restored_rule": rule_id, "rule_type": "placement"}
    assert restored is not None
    assert restored.rule_text == "Jewelry should go in B001."
    assert restored.trigger_terms == ["jewelry"]
    assert restored.target_location_id == "B001"
    assert restored.source_command == "old"
    assert restored.confidence == 0.4


def test_assistant_undo_restores_latest_confirmed_update_size_rule_plan(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(cfg.database_path)
    rule_id = store.learn_size_rule(
        rule_text="Jewelry boxes are medium.",
        trigger_terms=["jewelry", "box"],
        size_class="medium",
        source_command="old",
        confidence=0.5,
    )

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "Actually watch boxes are small",
            "plan": [
                {
                    "tool": "update_size_rule",
                    "arguments": {
                        "rule_id": rule_id,
                        "rule_text": "Watch boxes are small.",
                        "trigger_terms": ["watch", "box"],
                        "size_class": "small",
                        "source_command": "new",
                        "confidence": 0.95,
                    },
                }
            ],
        },
    ).json()

    assert confirmed["applied"][0]["previous_size_class"] == "medium"
    assert InventoryStore(cfg.database_path).matching_size_rules("watch box")[0].size_class == "small"

    undone = client.post("/assistant/undo").json()
    restored = InventoryStore(cfg.database_path).size_rule(rule_id)

    assert undone["ok"] is True
    assert undone["undone"][0] == {"restored_rule": rule_id, "rule_type": "size"}
    assert restored is not None
    assert restored.rule_text == "Jewelry boxes are medium."
    assert set(restored.trigger_terms) == {"jewelry", "box"}
    assert restored.size_class == "medium"
    assert restored.source_command == "old"
    assert restored.confidence == 0.5


def test_assistant_undo_reverses_move_item_and_created_empty_location(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    added = client.post("/inventory", json={"name": "Allen keys", "bin_id": "B001"}).json()
    item_id = added["item_id"]
    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "move Allen keys to a big tool bin",
            "plan": [
                {
                    "tool": "create_location",
                    "arguments": {
                        "type": "bin",
                        "label": "Big Tool Bin",
                        "parent_id": "main-cabinet",
                    },
                },
                {
                    "tool": "move_item",
                    "arguments": {
                        "item_id": item_id,
                        "item": "Allen keys",
                        "from_location_id": "B001",
                        "to_location_id": "big-tool-bin",
                    },
                },
            ],
        },
    ).json()
    assert confirmed["applied"][1]["moved_item"] == item_id
    assert client.get("/locations/big-tool-bin").status_code == 200

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    assert any(entry.get("moved_item") == item_id for entry in undone["undone"])
    assert any(entry.get("deleted_location") == "big-tool-bin" for entry in undone["undone"])
    assert client.get("/locations/B001").json()["items"][0]["item_id"] == item_id
    assert client.get("/locations/big-tool-bin").status_code == 404


def test_assistant_undo_reverses_move_container(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post("/locations", json={"type": "pouch", "label": "Kit Bag", "parent_id": "B001"})

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "move kit bag to B002",
            "plan": [
                {
                    "tool": "move_container",
                    "arguments": {"location_id": "kit-bag", "new_parent_id": "B002"},
                }
            ],
        },
    ).json()

    assert confirmed["applied"][0]["moved_container"] == "kit-bag"
    assert confirmed["applied"][0]["from_parent_id"] == "B001"
    assert client.get("/locations/kit-bag").json()["location"]["parent_id"] == "B002"

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    assert undone["undone"][0]["moved_container"] == "kit-bag"
    assert undone["message"] == "Moved Kit Bag back inside Test › B001."
    assert client.get("/locations/kit-bag").json()["location"]["parent_id"] == "B001"


def test_assistant_undo_describes_manual_item_move(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=2,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(database_path),
            )
        )
    )
    item_id = client.post(
        "/inventory", json={"name": "USB adapter", "bin_id": "B001"}
    ).json()["item_id"]

    moved = client.post(
        "/inventory/move",
        json={"item_id": item_id, "from_bin": "B001", "to_bin": "B002"},
    )
    assert moved.status_code == 200

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    assert undone["message"] == "Moved USB adapter back to B001."
    restored = InventoryStore(database_path).items_in_location("B001")
    assert [row.item_id for row in restored] == [item_id]


def test_assistant_undo_describes_manual_location_archive_and_restore(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 0, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    client.post("/locations", json={"type": "pouch", "label": "Kit Bag"})

    assert client.post("/locations/kit-bag/archive").status_code == 200
    restored = client.post("/assistant/undo").json()
    assert restored["message"] == "Restored Kit Bag from the archive."

    assert client.post("/locations/kit-bag/archive").status_code == 200
    assert client.post("/locations/kit-bag/restore").status_code == 200
    rearchived = client.post("/assistant/undo").json()
    assert rearchived["message"] == "Returned Kit Bag to the archive."
    assert client.get("/locations/kit-bag").status_code == 404


def test_voice_attempts_can_be_recorded_and_read_back(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    build = client.get("/build").json()
    current_runtime = f"v{build['version']} {build['commit'][:7]} test"

    response = client.post(
        "/voice/attempts",
        json={
            "rms": 400,
            "activity_rms": 812,
            "transcript": "Magpie find M14",
            "wake_word": "magpie",
            "wake_heard": True,
            "awaiting_command": False,
            "command": "find M14",
            "route": "/assistant/find",
            "query": "M4 T-nuts",
            "stt_model": "whisper-1",
            "audio_threshold": 500,
            "device_id": "main-cabinet",
            "runtime_version": current_runtime,
            "backend_response": {"lit": True},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["attempt_id"] > 0
    assert body["linked"] is False
    attempts = client.get("/voice/recent?limit=1").json()["attempts"]
    assert attempts[0]["rms"] == 400
    assert attempts[0]["activity_rms"] == 812
    assert attempts[0]["transcript"] == "Magpie find M14"
    assert attempts[0]["query"] == "M4 T-nuts"
    assert attempts[0]["device_id"] == "main-cabinet"
    assert attempts[0]["runtime_version"] == current_runtime
    assert attempts[0]["backend_response"] == {"lit": True}
    assert attempts[0]["tuning"]["checks"]["audio"] is True
    assert attempts[0]["tuning"]["activity_rms"] == 812
    assert attempts[0]["tuning"]["checks"]["routed"] is True
    assert attempts[0]["tuning"]["lit"] is True

    tuning = client.get("/voice/tuning?limit=1").json()
    assert tuning["ok"] is True
    assert tuning["count"] == 1
    assert tuning["attempts"][0]["tuning"]["checks"]["backend"] is True
    assert tuning["summary"] == {
        "activations": 1,
        "routed": 1,
        "routing_rate": 1.0,
        "backend_succeeded": 1,
        "backend_success_rate": 1.0,
        "passed_end_to_end": 0,
        "end_to_end_rate": 0.0,
        "wake_only": 0,
        "errors": 0,
    }
    acceptance = client.get("/voice/acceptance").json()
    assert acceptance["schema"] == "magpie.voice_acceptance.v1"
    assert acceptance["ready"] is False
    assert acceptance["window_size"] == 5
    assert acceptance["required_passes"] == 4
    assert acceptance["prompt"] == "Store an item in a lit location"
    assert acceptance["failures"] == [
        {"reason": "result was not linked to the kiosk", "count": 1}
    ]
    inventory = InventoryStore(tmp_path / "inventory.db")
    item_id = inventory.upsert_item(name="Blue pry tool", category="tools")
    inventory.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")
    assert client.get("/voice/acceptance").json()["prompt"] == (
        "Magpie, find Blue pry tool"
    )

    calibration = client.get("/voice/calibration?limit=1").json()
    assert calibration["ok"] is True
    assert calibration["calibration"]["quiet_samples"] == 0
    assert calibration["calibration"]["speech_samples"] == 1
    assert calibration["calibration"]["speech_floor_rms"] == 812
    assert calibration["calibration"]["next_step"] == (
        "Capture a quiet-room clip so Magpie knows the noise floor."
    )

    client.post(
        "/voice/attempts",
        json={
            "rms": 100,
            "activity_rms": 120,
            "wake_word": "magpie",
            "audio_threshold": 500,
            "error": "quiet",
        },
    )
    assert client.get("/voice/tuning?limit=10").json()["count"] == 1


def test_screen_heartbeat_reports_kiosk_liveness(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    empty = client.get("/screen/status").json()["screen"]
    assert empty["active"] is False
    assert empty["last_seen_interaction_id"] == 0

    kiosk_state = client.get(
        "/kiosk/state?wait_seconds=0&screen_id=dedicated-kiosk"
        "&screen_url=http%3A%2F%2F127.0.0.1%3A8765%2Fkiosk"
        "&build_label=v0.1.0%20kiosk&last_seen_interaction_id=21"
    )
    assert kiosk_state.status_code == 200
    current_build = client.get("/build").json()["label"]
    assert kiosk_state.json()["service_build_label"] == current_build
    assert kiosk_state.json()["reload_required"] is True
    current_kiosk_state = client.get(
        "/kiosk/state",
        params={"wait_seconds": 0, "build_label": current_build},
    ).json()
    assert current_kiosk_state["reload_required"] is False
    polled_screen = client.get("/screen/status").json()["screen"]
    assert polled_screen["active"] is True
    assert polled_screen["screen_id"] == "dedicated-kiosk"
    assert polled_screen["url"] == "http://127.0.0.1:8765/kiosk"
    assert polled_screen["build_label"] == "v0.1.0 kiosk"
    assert polled_screen["last_seen_interaction_id"] == 21

    response = client.post(
        "/screen/heartbeat",
        json={
            "screen_id": "cabinet-kiosk",
            "url": "http://127.0.0.1:8765/",
            "build_label": "v0.1.0 test",
            "last_seen_interaction_id": 42,
            "status": "active",
        },
    )

    assert response.status_code == 200
    screen = response.json()["screen"]
    assert screen["active"] is True
    assert screen["screen_id"] == "cabinet-kiosk"
    assert screen["last_seen_interaction_id"] == 42
    assert screen["build_label"] == "v0.1.0 test"

    local_capabilities = client.get("/edge/local-capabilities").json()
    assert local_capabilities == {
        "ok": True,
        "schema": "magpie.edge.local_capabilities.v1",
        "capabilities": ["set_pixels", "kiosk_display"],
        "services": {
            "lights": True,
            "voice": False,
            "screen": True,
            "printer": False,
            "check_station": False,
        },
    }


def test_voice_state_reports_live_listener_feedback(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    initial = client.get("/voice/status").json()["voice"]
    assert initial["state"] == "starting"
    assert initial["active"] is False

    response = client.post(
        "/voice/state",
        json={
            "state": "heard",
            "message": "Heard: put away M4 screws",
            "transcript": "Put away M4 screws.",
            "command": "put away M4 screws",
            "device_id": "main-cabinet",
            "runtime_version": "v0.1.0 abc1234",
            "capabilities": {
                "cloud_audio_available": False,
                "cloud_audio_failure": "quota_exhausted",
                "cloud_audio_retry_seconds": 1800,
                "local_stt_available": True,
                "local_tts_available": False,
                "wake_gate": "local",
                "audio_before_wake_uploaded": False,
                "no_wake_follow_up": "local_confirmation_only",
                "metrics_since": 1234.5,
                "speech_clips": 18,
                "passive_speech_ignored": 12,
                "wake_detections": 4,
                "cloud_stt_calls": 4,
                "local_stt_calls": 0,
                "agent_requests": 3,
                "cloud_tts_calls": 3,
                "local_tts_calls": 0,
                "secret": "must not persist",
            },
        },
    )

    assert response.status_code == 200
    voice = response.json()["voice"]
    assert voice["active"] is True
    assert voice["state"] == "heard"
    assert voice["command"] == "put away M4 screws"
    assert voice["device_id"] == "main-cabinet"
    assert voice["runtime_version"] == "v0.1.0 abc1234"
    assert voice["capabilities"] == {
        "cloud_audio_available": False,
        "cloud_audio_failure": "quota_exhausted",
        "cloud_audio_retry_seconds": 1800,
        "local_stt_available": True,
        "local_tts_available": False,
        "wake_gate": "local",
        "audio_before_wake_uploaded": False,
        "no_wake_follow_up": "local_confirmation_only",
        "metrics_since": 1234.5,
        "speech_clips": 18,
        "passive_speech_ignored": 12,
        "wake_detections": 4,
        "cloud_stt_calls": 4,
        "local_stt_calls": 0,
        "agent_requests": 3,
        "cloud_tts_calls": 3,
        "local_tts_calls": 0,
    }
    assert voice["last_result"] is None
    assert client.get("/edge/local-capabilities").json()["services"] == {
        "lights": True,
        "voice": True,
        "screen": False,
        "printer": False,
        "check_station": False,
    }

    client.post(
        "/voice/state",
        json={
            "state": "done",
            "message": "Confirmed. Stored M4 screws in B001.",
            "transcript": "yes",
            "command": "confirm",
        },
    )
    waiting = client.post(
        "/voice/state",
        json={"state": "waiting", "message": "Waiting for Magpie"},
    ).json()["voice"]
    assert waiting["state"] == "waiting"
    assert waiting["device_id"] == "main-cabinet"
    assert waiting["runtime_version"] == "v0.1.0 abc1234"
    assert waiting["last_result"]["state"] == "done"
    assert waiting["last_result"]["message"] == "Confirmed. Stored M4 screws in B001."
    assert waiting["last_result"]["age_seconds"] >= 0

    invalid = client.post("/voice/state", json={"state": "confused"})
    assert invalid.status_code == 400


def test_kiosk_state_combines_voice_and_interactions_with_idle_long_poll(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(database_path),
    )
    client = TestClient(create_app(cfg))
    interaction_id = InventoryStore(database_path).record_assistant_interaction(
        route="/assistant/chat",
        command="find M4 T-nuts",
        response={"action": "find", "results": []},
        status="completed",
    )
    voice = client.post(
        "/voice/state",
        json={"state": "heard", "message": "Heard M4 T-nuts"},
    ).json()["voice"]

    combined_response = client.get(
        "/kiosk/state",
        params={
            "after_id": interaction_id - 1,
            "voice_updated_at": -1,
            "wait_seconds": 0,
        },
    )
    assert combined_response.headers["cache-control"] == "no-store"
    combined = combined_response.json()
    assert combined["voice"]["state"] == "heard"
    assert combined["storage"] == {
        "schema": "magpie.kiosk_storage.v1",
        "configured": True,
        "put_away_ready": True,
        "location_count": 2,
        "destination_count": 1,
        "root_count": 1,
        "setup_path": "/storage?view=places&onboarding=destination",
    }
    assert combined["interactions"][0]["id"] == interaction_id
    assert combined["interactions"][0]["voice_confirmable"] is False
    assert combined["pending_confirmation"] is None
    assert combined["pending_agent_plan"] is None
    assert combined["confirmation_state"] is None
    assert combined["latest_id"] == interaction_id
    assert combined["timed_out"] is False

    complex_id = InventoryStore(database_path).record_assistant_interaction(
        route="/assistant/chat",
        command="move the hardware and label the drawer",
        response={
            "action": "proposed",
            "plan": [
                {
                    "tool": "move_item",
                    "arguments": {
                        "item": "M4 T-nuts",
                        "from_location_id": "B001",
                        "to_location_id": "main-cabinet",
                    },
                },
                {
                    "tool": "print_label",
                    "arguments": {"location_id": "main-cabinet"},
                },
            ],
        },
        status="pending",
    )
    complex_state = client.get(
        "/kiosk/state",
        params={
            "after_id": complex_id,
            "confirmation_interaction_id": complex_id,
            "voice_updated_at": voice["updated_at"],
            "wait_seconds": 0,
        },
    ).json()
    assert complex_state["pending_agent_plan"]["id"] == complex_id
    assert complex_state["pending_confirmation"] is None
    assert complex_state["confirmation_state"] == {
        "interaction_id": complex_id,
        "status": "pending",
        "confirmable": True,
    }

    InventoryStore(database_path).set_assistant_interaction_status(
        complex_id,
        "superseded",
    )
    terminal_state = client.get(
        "/kiosk/state",
        params={
            "after_id": complex_id,
            "confirmation_interaction_id": complex_id,
            "voice_updated_at": voice["updated_at"],
            "wait_seconds": 1,
        },
    ).json()
    assert terminal_state["pending_agent_plan"] is None
    assert terminal_state["confirmation_state"] == {
        "interaction_id": complex_id,
        "status": "superseded",
        "confirmable": False,
    }
    assert terminal_state["timed_out"] is False

    pending_id = InventoryStore(database_path).record_assistant_interaction(
        route="/assistant/chat",
        command="put away two M4 T-nuts",
        response={
            "action": "proposed",
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "M4 T-nuts",
                        "quantity": 2,
                        "location_id": "B001",
                    },
                }
            ],
        },
        status="pending",
    )
    pending_state = client.get(
        "/kiosk/state",
        params={
            "after_id": pending_id,
            "voice_updated_at": voice["updated_at"],
            "wait_seconds": 0,
        },
    ).json()
    assert pending_state["pending_agent_plan"]["id"] == pending_id
    pending_state = pending_state["pending_confirmation"]
    assert pending_state["id"] == pending_id
    presentation = pending_state["response"]["confirmation"]
    assert presentation["schema"] == "magpie.confirmation.v1"
    assert presentation["title"] == "Ready to confirm"
    assert presentation["instruction"] == (
        "Review the destination, then confirm or change it."
    )
    assert presentation["voice_instruction"] == (
        "Say yes to confirm, no to cancel, or say what to change."
    )
    assert presentation["actions"] == [
        {"id": "confirm", "label": "Confirm"},
        {"id": "change", "label": "Change"},
        {"id": "cancel", "label": "Cancel"},
    ]
    assert presentation["change_prompts"] == [
        {
            "id": "different_place",
            "label": "Different place",
            "text": "Use a different storage place.",
        },
        {
            "id": "bigger_container",
            "label": "Bigger container",
            "text": "Use a bigger bin or container.",
        },
        {
            "id": "smaller_container",
            "label": "Smaller container",
            "text": "Use a smaller bin or container.",
        },
        {
            "id": "wrong_item",
            "label": "Wrong item",
            "text": "The item identity is wrong.",
        },
    ]
    assert presentation["rows"] == [
        {
            "index": 0,
            "tool": "put_item",
            "summary": "Store 2 x M4 T-nuts",
            "destination": {
                "id": "B001",
                "path": [
                    {"id": "main-cabinet", "label": "Test"},
                    {"id": "B001", "label": "B001"},
                ],
                "label": "Test › B001",
                "light_location_id": "B001",
            },
            "color": {"name": "Red", "rgb": [255, 0, 0]},
            "light": {"location_id": "B001", "status": "available"},
        }
    ]
    InventoryStore(database_path).set_assistant_interaction_status(
        pending_id,
        "canceled",
    )

    idle = client.get(
        "/kiosk/state",
        params={
            "after_id": pending_id,
            "voice_updated_at": voice["updated_at"],
            "wait_seconds": 0.01,
        },
    ).json()
    assert idle["interactions"] == []
    assert idle["pending_confirmation"] is None
    assert idle["latest_id"] == pending_id
    assert idle["timed_out"] is True

    with TestClient(create_app(cfg)) as live_client:
        live_voice = live_client.post(
            "/voice/state",
            json={"state": "heard", "message": "Heard M4 T-nuts"},
        ).json()["voice"]
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=1) as executor:
            waiting = executor.submit(
                live_client.get,
                "/kiosk/state",
                params={
                    "after_id": pending_id,
                    "voice_updated_at": live_voice["updated_at"],
                    "wait_seconds": 2,
                },
            )
            time.sleep(0.05)
            live_client.post(
                "/voice/state",
                json={"state": "processing", "message": "Finding M4 T-nuts"},
            )
            changed = waiting.result(timeout=1).json()
    assert time.monotonic() - started < 1
    assert changed["timed_out"] is False
    assert changed["voice"]["state"] == "processing"


def test_kiosk_state_keeps_confirming_proposal_nonterminal(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "software-only",
                "columns": 1,
                "rows_bottom_to_top": [],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    store = InventoryStore(database_path)
    interaction_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="put away M4 bolts",
        response={
            "action": "proposed",
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "M4 bolts",
                        "location_id": "parts-bin",
                    },
                }
            ],
        },
        status="pending",
    )
    store.set_assistant_interaction_status(interaction_id, "confirming")
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(database_path),
            )
        )
    )

    state = client.get(
        "/kiosk/state",
        params={
            "after_id": interaction_id,
            "confirmation_interaction_id": interaction_id,
            "voice_updated_at": 0,
            "wait_seconds": 0.01,
        },
    ).json()

    assert state["pending_agent_plan"] is None
    assert state["confirmation_state"] == {
        "interaction_id": interaction_id,
        "status": "confirming",
        "confirmable": False,
    }
    assert state["timed_out"] is True


def test_kiosk_state_reports_visual_storage_setup_readiness(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "software-only",
                "columns": 1,
                "rows_bottom_to_top": [],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )

    empty = client.get("/kiosk/state?wait_seconds=0").json()["storage"]
    assert empty == {
        "schema": "magpie.kiosk_storage.v1",
        "configured": False,
        "put_away_ready": False,
        "location_count": 0,
        "destination_count": 0,
        "root_count": 0,
        "setup_path": "/storage?onboarding=1",
    }

    root = client.post(
        "/locations",
        json={"type": "room", "label": "Garage", "accepts_items": False},
    ).json()["location"]
    hierarchy_only = client.get("/kiosk/state?wait_seconds=0").json()["storage"]
    assert hierarchy_only == {
        "schema": "magpie.kiosk_storage.v1",
        "configured": True,
        "put_away_ready": False,
        "location_count": 1,
        "destination_count": 0,
        "root_count": 1,
        "setup_path": "/storage?view=places&onboarding=destination",
    }

    client.post(
        "/locations",
        json={
            "type": "bin",
            "label": "General hardware",
            "parent_id": root["id"],
            "accepts_items": True,
        },
    )
    ready = client.get("/kiosk/state?wait_seconds=0").json()["storage"]
    assert ready["configured"] is True
    assert ready["put_away_ready"] is True
    assert ready["location_count"] == 2
    assert ready["destination_count"] == 1
    assert ready["root_count"] == 1


def test_kiosk_storage_readiness_is_shared_until_storage_changes(
    tmp_path, monkeypatch
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "software-only",
                "columns": 1,
                "rows_bottom_to_top": [],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    original_all = LocationStore.all
    calls = 0

    def counted_all(store, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original_all(store, *args, **kwargs)

    monkeypatch.setattr(LocationStore, "all", counted_all)

    client.post(
        "/voice/state",
        json={"state": "waiting", "message": "Waiting for Magpie"},
    )
    first = client.get("/kiosk/state?wait_seconds=0").json()
    after_first = calls
    second = client.get("/kiosk/state?wait_seconds=0").json()

    assert after_first == 1
    assert calls == after_first
    assert first["storage"] == second["storage"]
    idle = client.get(
        "/kiosk/state",
        params={
            "after_id": first["latest_id"],
            "voice_updated_at": first["voice"]["updated_at"],
            "wait_seconds": 0.01,
        },
    ).json()
    assert idle["timed_out"] is True
    assert calls == after_first

    client.post(
        "/voice/state",
        json={"state": "heard", "message": "Heard M4 T-nuts"},
    )
    client.get("/kiosk/state?wait_seconds=0")
    assert calls == after_first

    created = client.post(
        "/locations",
        json={"type": "bin", "label": "Hardware", "accepts_items": True},
    )
    assert created.status_code == 200
    after_write = calls
    refreshed = client.get("/kiosk/state?wait_seconds=0").json()["storage"]

    assert calls == after_write + 1
    assert refreshed["configured"] is True
    assert refreshed["put_away_ready"] is True
    assert refreshed["location_count"] == 1


def test_kiosk_and_voice_do_not_confirm_place_management_plans(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(database_path),
            )
        )
    )
    store = InventoryStore(database_path)
    plan = [
        {
            "tool": "create_location",
            "arguments": {"type": "room", "label": "Attic"},
        }
    ]
    interaction_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="create a place called Attic",
        response={"action": "proposed", "plan": plan},
        status="pending",
    )

    kiosk = client.get(
        "/kiosk/state",
        params={"after_id": 0, "voice_updated_at": -1, "wait_seconds": 0},
    ).json()
    voice_confirm = client.post("/assistant/voice/confirm").json()
    kiosk_confirm = client.post("/kiosk/actions/confirm").json()
    kiosk_revise = client.post(
        "/kiosk/actions/revise",
        json={"prompt_id": "different_place"},
    ).json()

    assert kiosk["pending_confirmation"] is None
    structural = [
        interaction
        for interaction in kiosk["interactions"]
        if interaction["id"] == interaction_id
    ][0]
    assert structural["voice_confirmable"] is False
    assert voice_confirm == {"ok": False, "message": VOICE_SCOPE_MESSAGE}
    assert kiosk_confirm == {"ok": False, "message": VOICE_SCOPE_MESSAGE}
    assert kiosk_revise == {"ok": False, "message": VOICE_SCOPE_MESSAGE}
    assert store.assistant_interaction(interaction_id)["status"] == "pending"

    web_confirm = client.post(
        "/assistant/confirm",
        json={
            "command": "create a place called Attic",
            "plan": plan,
            "source_interaction_id": interaction_id,
        },
    )
    assert web_confirm.status_code == 200
    assert web_confirm.json()["applied"][0]["created_location"] == "attic"


def test_voice_attempt_links_back_to_assistant_interaction(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(db_path)
    interaction_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="find M4 T-nuts",
        response={
            "ok": True,
            "mode": "find",
            "message": "Found 1 result.",
            "interaction_id": 99,
        },
    )

    response = client.post(
        "/voice/attempts",
        json={
            "rms": 910,
            "transcript": "Magpie find M14",
            "wake_word": "magpie",
            "wake_heard": True,
            "command": "find M14",
            "route": "/assistant/chat",
            "query": "find M4 T-nuts",
            "stt_model": "gpt-4o-mini-transcribe",
            "audio_threshold": 500,
            "backend_response": {"ok": True, "interaction_id": interaction_id},
        },
    )

    body = response.json()
    assert body["ok"] is True
    assert body["linked"] is True
    assert body["linked_interaction_id"] == interaction_id
    interaction = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    voice_attempt = interaction["response"]["voice_attempt"]
    assert voice_attempt["id"] == body["attempt_id"]
    assert voice_attempt["transcript"] == "Magpie find M14"
    assert voice_attempt["query"] == "find M4 T-nuts"
    assert voice_attempt["stt_model"] == "gpt-4o-mini-transcribe"
    assert voice_attempt["rms"] == 910

    client.post(
        "/screen/heartbeat",
        json={
            "screen_id": "cabinet-kiosk",
            "url": "http://127.0.0.1:8765/",
            "build_label": "test",
            "last_seen_interaction_id": interaction_id,
            "status": "active",
        },
    )
    tuning = client.get("/voice/tuning?limit=1").json()
    assert tuning["passed"] == 1
    assert tuning["failed"] == 0
    row = tuning["attempts"][0]["tuning"]
    assert row["passed"] is True
    assert row["linked_interaction_id"] == interaction_id
    assert row["screen_seen_interaction"] is True
    assert row["screen_observation"]["screen_id"] == "cabinet-kiosk"
    assert row["screen_observation"]["build_label"] == "test"
    assert row["screen"]["active"] is True
    assert row["checks"] == {
        "audio": True,
        "wake_or_armed": True,
        "command": True,
        "routed": True,
        "backend": True,
        "screen_linked": True,
        "screen_active": True,
        "screen_observed": True,
        "light": True,
    }
    client.post(
        "/screen/heartbeat",
        json={
            "screen_id": "cabinet-kiosk",
            "url": "http://127.0.0.1:8765/",
            "build_label": "newer-heartbeat",
            "last_seen_interaction_id": interaction_id,
            "status": "active",
        },
    )
    assert store.assistant_interaction(interaction_id)["response"][
        "screen_observed"
    ]["build_label"] == "test"


def test_voice_confirmation_attempt_links_to_completion_receipt(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=0,
        backend="none",
        layout_path=str(layout_path),
        database_path=str(database_path),
    )
    store = InventoryStore(database_path)
    proposal_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="put away M4 T-nuts",
        response={"ok": True, "plan": [{"tool": "put_item"}]},
        status="confirmed",
    )
    confirmation_id = store.record_assistant_interaction(
        route="/assistant/voice/confirm",
        command="put away M4 T-nuts",
        response={
            "ok": True,
            "source_interaction_id": proposal_id,
            "receipt": {"summary": "Stored M4 T-nuts."},
        },
        status="confirmed",
    )
    client = TestClient(create_app(cfg))

    result = client.post(
        "/voice/attempts",
        json={
            "rms": 900,
            "transcript": "yes",
            "wake_word": "magpie",
            "wake_heard": True,
            "command": "confirm",
            "route": "/assistant/voice/confirm",
            "query": "confirm",
            "audio_threshold": 500,
            "backend_response": {
                "ok": True,
                "source_interaction_id": proposal_id,
                "confirmation_interaction_id": confirmation_id,
            },
        },
    ).json()

    assert result["linked"] is True
    assert result["linked_interaction_id"] == confirmation_id
    assert "voice_attempt" not in store.assistant_interaction(proposal_id)["response"]
    confirmation = store.assistant_interaction(confirmation_id)
    assert confirmation["response"]["voice_attempt"]["command"] == "confirm"


def test_newer_kiosk_activity_does_not_retroactively_validate_old_voice_attempt(
    tmp_path,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=0,
        backend="none",
        layout_path=str(layout_path),
        database_path=str(database_path),
    )
    store = InventoryStore(database_path)
    old_interaction_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="list my storage spaces",
        response={"ok": True, "message": "Listed storage."},
    )
    newer_interaction_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="find USB drives",
        response={"ok": True, "message": "Found USB drives."},
    )
    client = TestClient(create_app(cfg))

    client.post(
        "/voice/attempts",
        json={
            "rms": 900,
            "transcript": "Magpie list my storage spaces",
            "wake_word": "magpie",
            "wake_heard": True,
            "command": "list my storage spaces",
            "route": "/assistant/chat",
            "query": "list my storage spaces",
            "audio_threshold": 500,
            "backend_response": {"ok": True, "interaction_id": old_interaction_id},
        },
    )
    client.post(
        "/screen/heartbeat",
        json={
            "screen_id": "cabinet-kiosk",
            "build_label": "test",
            "last_seen_interaction_id": newer_interaction_id,
            "status": "active",
        },
    )

    [attempt] = client.get("/voice/tuning?limit=1").json()["attempts"]
    assert attempt["tuning"]["screen_seen_interaction"] is False
    assert attempt["tuning"]["checks"]["screen_active"] is False
    assert attempt["tuning"]["checks"]["screen_observed"] is False
    assert store.assistant_interaction(old_interaction_id)["response"].get(
        "screen_observed"
    ) is None
    assert store.assistant_interaction(newer_interaction_id)["response"][
        "screen_observed"
    ]["screen_id"] == "cabinet-kiosk"


def test_voice_calibration_recommends_threshold_from_quiet_and_speech_samples(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post(
        "/voice/attempts",
        json={
            "rms": 120,
            "transcript": "",
            "wake_word": "magpie",
            "wake_heard": False,
            "command": "",
            "route": "",
            "query": "",
            "stt_model": "gpt-4o-mini-transcribe",
            "audio_threshold": 800,
            "backend_response": {},
            "error": "quiet",
        },
    )
    client.post(
        "/voice/attempts",
        json={
            "rms": 1180,
            "transcript": "Magpie find M4 T-nuts",
            "wake_word": "magpie",
            "wake_heard": True,
            "command": "find M4 T-nuts",
            "route": "/assistant/chat",
            "query": "find M4 T-nuts",
            "stt_model": "gpt-4o-mini-transcribe",
            "audio_threshold": 800,
            "backend_response": {"ok": True, "interaction_id": 77, "automation": {"lit": True}},
        },
    )

    calibration = client.get("/voice/calibration?limit=10").json()["calibration"]

    assert calibration["ready"] is True
    assert calibration["quiet_samples"] == 1
    assert calibration["speech_samples"] == 1
    assert calibration["quiet_floor_rms"] == 120
    assert calibration["speech_floor_rms"] == 1180
    assert calibration["suggested_threshold"] == 650
    assert calibration["next_step"] == (
        "Say 'Magpie, find M4 T-nuts' near the cabinet to run the live acceptance test."
    )


def test_voice_calibration_recommends_change_when_threshold_is_outside_observed_range(
    tmp_path,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    for payload in (
        {
            "rms": 1180,
            "transcript": "Magpie find M4 T-nuts",
            "wake_word": "magpie",
            "wake_heard": True,
            "command": "find M4 T-nuts",
            "route": "/assistant/chat",
            "query": "find M4 T-nuts",
            "stt_model": "gpt-4o-transcribe",
            "audio_threshold": 800,
            "backend_response": {"ok": True, "interaction_id": 77},
        },
        {
            "rms": 120,
            "transcript": "",
            "wake_word": "magpie",
            "wake_heard": False,
            "command": "",
            "route": "",
            "query": "",
            "stt_model": "gpt-4o-transcribe",
            "audio_threshold": 1500,
            "backend_response": {},
            "error": "quiet",
        },
    ):
        client.post("/voice/attempts", json=payload)

    calibration = client.get("/voice/calibration?limit=10").json()["calibration"]

    assert calibration["ready"] is True
    assert calibration["suggested_threshold"] == 650
    assert calibration["next_step"] == (
        "Set MAGPIE_VOICE_AUDIO_THRESHOLD near the suggested threshold and retest."
    )


def test_voice_calibration_reports_unclear_above_threshold_audio(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post(
        "/voice/attempts",
        json={
            "rms": 120,
            "transcript": "",
            "wake_word": "magpie",
            "wake_heard": False,
            "command": "",
            "route": "",
            "query": "",
            "stt_model": "gpt-4o-mini-transcribe",
            "audio_threshold": 800,
            "backend_response": {},
            "error": "quiet",
        },
    )
    client.post(
        "/voice/attempts",
        json={
            "rms": 920,
            "transcript": "",
            "wake_word": "magpie",
            "wake_heard": False,
            "command": "",
            "route": "",
            "query": "",
            "stt_model": "gpt-4o-mini-transcribe",
            "audio_threshold": 800,
            "backend_response": {},
            "error": "no_transcript",
        },
    )

    calibration = client.get("/voice/calibration?limit=10").json()["calibration"]

    assert calibration["ready"] is False
    assert calibration["quiet_samples"] == 1
    assert calibration["speech_samples"] == 0
    assert calibration["unclear_samples"] == 1
    assert calibration["ambient_samples"] == 0
    assert calibration["unclear_floor_rms"] == 920
    assert calibration["next_step"] == (
        "Audio is crossing the threshold but STT is not returning speech; "
        "move closer, reduce noise, or compare STT models."
    )


def test_voice_calibration_reports_transcribed_room_speech_as_ambient(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post(
        "/voice/attempts",
        json={
            "rms": 120,
            "transcript": "",
            "wake_word": "magpie",
            "wake_heard": False,
            "command": "",
            "route": "",
            "query": "",
            "stt_model": "gpt-4o-transcribe",
            "audio_threshold": 800,
            "backend_response": {},
            "error": "quiet",
        },
    )
    client.post(
        "/voice/attempts",
        json={
            "rms": 920,
            "transcript": "Okay, and then the yellow switch.",
            "wake_word": "magpie",
            "wake_heard": False,
            "command": "",
            "route": "",
            "query": "",
            "stt_model": "gpt-4o-transcribe",
            "audio_threshold": 800,
            "backend_response": {},
            "error": "",
        },
    )

    calibration = client.get("/voice/calibration?limit=10").json()["calibration"]

    assert calibration["ready"] is False
    assert calibration["speech_samples"] == 0
    assert calibration["unclear_samples"] == 0
    assert calibration["ambient_samples"] == 1
    assert calibration["ambient_floor_rms"] == 920
    assert calibration["next_step"] == "Say a real command near the cabinet to capture speech RMS."


def test_voice_calibration_ignores_threshold_bypass_diagnostics(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    client.post(
        "/voice/attempts",
        json={
            "rms": 430,
            "activity_rms": 480,
            "stt_model": "gpt-4o-mini-transcribe",
            "audio_threshold": 0,
            "error": "no_transcript",
        },
    )
    client.post(
        "/voice/attempts",
        json={
            "rms": 450,
            "activity_rms": 510,
            "stt_model": "gpt-4o-mini-transcribe",
            "audio_threshold": 800,
            "error": "quiet",
        },
    )

    calibration = client.get("/voice/calibration?limit=10").json()["calibration"]

    assert calibration["current_threshold"] == 800
    assert calibration["quiet_samples"] == 1
    assert calibration["unclear_samples"] == 0
    assert calibration["quiet_floor_rms"] == 510


def test_demo_readiness_reports_voice_evidence_gap(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    build = client.get("/build").json()

    readiness = client.get("/demo/readiness").json()

    assert readiness["ok"] is True
    assert readiness["schema"] == "magpie.demo_readiness.v1"
    assert readiness["ready"] is False
    checks = {check["key"]: check for check in readiness["checks"]}
    assert checks["qr_scan"]["passed"] is True
    assert checks["text_photo_putaway"]["passed"] is True
    assert checks["voice_find"]["passed"] is False
    assert "This build has no voice evidence" in checks["voice_find"]["next_step"]
    assert readiness["voice_window"] == {
        "size": 0,
        "passed": 0,
        "rate": 0.0,
        "lit_find": False,
        "runtime_scope": build["commit"][:7] or "unknown",
        "evidence_state": "missing_current",
        "legacy_attempt_count": 0,
        "required_size": 5,
        "required_passes": 4,
        "required_rate": 0.8,
    }
    assert checks["build_visibility"]["passed"] is True
    assert checks["cabinet_hardware"]["passed"] is True
    assert readiness["hardware"]["ready"] is True


def test_demo_readiness_passes_after_voice_find_evidence(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(database_path),
    )
    store = InventoryStore(database_path)
    client = TestClient(create_app(cfg))
    build = client.get("/build").json()
    current_runtime = f"v{build['version']} {build['commit'][:7]} test"
    for command, lit, activity_rms, backend_ok in (
        ("what is in PrinterCab", False, 1100, True),
        ("list my storage rules", False, 400, False),
        ("find M4 T-nuts", True, 1100, True),
        ("list my storage spaces", False, 1100, True),
        ("what is in the main cabinet", False, 1100, True),
    ):
        interaction_id = store.record_assistant_interaction(
            route="/assistant/chat",
            command=command,
            response={"ok": backend_ok, "automation": {"lit": lit}},
        )
        client.post(
            "/voice/attempts",
            json={
                "rms": 1100,
                "activity_rms": activity_rms,
                "transcript": f"Magpie {command}",
                "wake_word": "magpie",
                "wake_heard": True,
                "command": command,
                "route": "/assistant/chat",
                "query": command,
                "stt_model": "gpt-4o-mini-transcribe",
                "audio_threshold": 800,
                "runtime_version": current_runtime,
                "backend_response": {
                    "ok": backend_ok,
                    "interaction_id": interaction_id,
                    "automation": {"lit": lit},
                },
            },
        )
        if backend_ok:
            client.post(
                "/screen/heartbeat",
                json={
                    "screen_id": "cabinet-kiosk",
                    "url": "http://127.0.0.1:8765/",
                    "build_label": "test",
                    "last_seen_interaction_id": interaction_id,
                    "status": "active",
                },
            )
    proposal_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="put away test washers",
        response={"ok": True, "automation": {"lit": True}},
        status="confirmed",
    )
    confirmation_id = store.record_assistant_interaction(
        route="/assistant/voice/confirm",
        command="confirm",
        response={
            "ok": True,
            "source_interaction_id": proposal_id,
            "applied": [{"stored": "test washers", "location_id": "B001"}],
        },
        status="confirmed",
    )
    store.update_assistant_interaction_response(
        proposal_id,
        {
            "confirmation_response": {
                "ok": True,
                "confirmation_interaction_id": confirmation_id,
                "applied": [{"stored": "test washers", "location_id": "B001"}],
            }
        },
    )
    for interaction_id, command, route, backend_response in (
        (
            proposal_id,
            "put away test washers",
            "/assistant/chat",
            {"ok": True, "interaction_id": proposal_id, "automation": {"lit": True}},
        ),
        (
            confirmation_id,
            "confirm",
            "/assistant/voice/confirm",
            {
                "ok": True,
                "source_interaction_id": proposal_id,
                "confirmation_interaction_id": confirmation_id,
            },
        ),
    ):
        client.post(
            "/voice/attempts",
            json={
                "rms": 1100,
                "activity_rms": 1100,
                "transcript": f"Magpie {command}",
                "wake_word": "magpie",
                "wake_heard": True,
                "command": command,
                "route": route,
                "query": command,
                "stt_model": "gpt-4o-mini-transcribe",
                "audio_threshold": 800,
                "runtime_version": current_runtime,
                "backend_response": backend_response,
            },
        )
        client.post(
            "/screen/heartbeat",
            json={
                "screen_id": "cabinet-kiosk",
                "url": "http://127.0.0.1:8765/",
                "build_label": "test",
                "last_seen_interaction_id": interaction_id,
                "status": "active",
            },
        )
    for _ in range(60):
        client.post(
            "/voice/attempts",
            json={
                "rms": 120,
                "activity_rms": 150,
                "stt_model": "gpt-4o-transcribe",
                "audio_threshold": 800,
                "error": "quiet",
            },
        )

    readiness = client.get("/demo/readiness").json()

    assert readiness["ready"] is True
    assert readiness["failed"] == 0
    checks = {check["key"]: check for check in readiness["checks"]}
    assert checks["voice_find"]["passed"] is True
    assert readiness["voice_window"]["rate"] == 1.0
    assert readiness["voice_window"]["lit_find"] is True
    assert readiness["voice_window"]["required_size"] == 5
    assert readiness["voice_window"]["required_passes"] == 4
    acceptance = client.get("/voice/acceptance").json()
    assert acceptance["schema"] == "magpie.voice_acceptance.v1"
    assert acceptance["ready"] is True
    assert acceptance["passed"] == 5
    assert acceptance["prompt"] == "Voice check passed"
    assert acceptance["failures"] == []
    assert acceptance["hands_free_confirmation"]["ready"] is True
    assert all(acceptance["hands_free_confirmation"]["checks"].values())
    assert acceptance["evidence_state"] == "current"


def test_voice_acceptance_scopes_attempts_to_current_runtime(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(database_path),
    )
    store = InventoryStore(database_path)
    interaction_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="find M4 T-nuts",
        response={"ok": True, "automation": {"lit": True}},
    )
    client = TestClient(create_app(cfg))
    build = client.get("/build").json()
    current_runtime = f"v{build['version']} {build['commit'][:7]} test"
    client.post(
        "/screen/heartbeat",
        json={
            "screen_id": "cabinet-kiosk",
            "url": "http://127.0.0.1:8765/",
            "build_label": "test",
            "last_seen_interaction_id": interaction_id,
            "status": "active",
        },
    )
    client.post(
        "/voice/attempts",
        json={
            "transcript": "clear lights",
            "wake_word": "magpie",
            "wake_heard": True,
            "audio_threshold": 800,
        },
    )
    client.post(
        "/voice/attempts",
        json={
            "rms": 1100,
            "transcript": "find M4 T-nuts",
            "wake_word": "magpie",
            "wake_heard": True,
            "command": "find M4 T-nuts",
            "route": "/assistant/chat",
            "query": "find M4 T-nuts",
            "audio_threshold": 800,
            "runtime_version": current_runtime,
            "backend_response": {
                "ok": True,
                "interaction_id": interaction_id,
                "automation": {"lit": True},
            },
        },
    )

    acceptance = client.get("/voice/acceptance").json()

    assert acceptance["runtime_scope"] == build["commit"][:7]
    assert acceptance["attempt_count"] == 1
    assert acceptance["passed"] == 1
    assert acceptance["lit_find"] is True
    assert acceptance["ready"] is False
    assert acceptance["attempts"][0]["runtime_version"] == current_runtime
    assert acceptance["legacy_attempt_count"] == 1
    assert acceptance["evidence_state"] == "current"


def test_voice_acceptance_bulk_loads_linked_interactions(
    tmp_path,
    monkeypatch,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    store = InventoryStore(database_path)
    interaction_ids = [
        store.record_assistant_interaction(
            route="/assistant/chat",
            command=f"find item {index}",
            response={"ok": True, "automation": {"lit": True}},
        )
        for index in range(5)
    ]
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(database_path),
            )
        )
    )
    build = client.get("/build").json()
    current_runtime = f"v{build['version']} {build['commit'][:7]} test"
    for interaction_id in interaction_ids:
        client.post(
            "/voice/attempts",
            json={
                "rms": 900,
                "transcript": "Magpie find item",
                "wake_word": "magpie",
                "wake_heard": True,
                "command": "find item",
                "route": "/assistant/chat",
                "query": "find item",
                "audio_threshold": 500,
                "runtime_version": current_runtime,
                "backend_response": {
                    "ok": True,
                    "interaction_id": interaction_id,
                    "automation": {"lit": True},
                },
            },
        )

    bulk_calls: list[list[int]] = []
    original_bulk = InventoryStore.assistant_interactions_by_ids

    def counted_bulk(self, requested_ids):
        bulk_calls.append(list(requested_ids))
        return original_bulk(self, requested_ids)

    monkeypatch.setattr(InventoryStore, "assistant_interactions_by_ids", counted_bulk)
    monkeypatch.setattr(
        InventoryStore,
        "assistant_interaction",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("voice acceptance must not fetch linked attempts one at a time")
        ),
    )

    response = client.get("/voice/acceptance")

    assert response.status_code == 200
    assert bulk_calls == [interaction_ids]


def test_voice_acceptance_does_not_treat_legacy_attempts_as_current(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    build = client.get("/build").json()
    client.post(
        "/voice/attempts",
        json={
            "rms": 1100,
            "transcript": "Magpie find M4 T-nuts",
            "wake_word": "magpie",
            "wake_heard": True,
            "command": "find M4 T-nuts",
            "route": "/assistant/chat",
            "query": "find M4 T-nuts",
            "audio_threshold": 800,
            "backend_response": {"ok": True, "automation": {"lit": True}},
        },
    )

    acceptance = client.get("/voice/acceptance").json()

    assert acceptance["runtime_scope"] == build["commit"][:7]
    assert acceptance["evidence_state"] == "missing_current"
    assert acceptance["legacy_attempt_count"] == 1
    assert acceptance["attempt_count"] == 0
    assert acceptance["passed"] == 0
    assert acceptance["lit_find"] is False
    assert acceptance["attempts"] == []
    assert "This build has no voice evidence" in acceptance["instruction"]


def test_demo_readiness_rejects_one_success_followed_by_failures(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(database_path),
    )
    store = InventoryStore(database_path)
    interaction_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="find M4 T-nuts",
        response={"ok": True, "automation": {"lit": True}},
    )
    client = TestClient(create_app(cfg))
    build = client.get("/build").json()
    current_runtime = f"v{build['version']} {build['commit'][:7]} test"
    client.post(
        "/voice/attempts",
        json={
            "rms": 1100,
            "transcript": "Magpie find M4 T-nuts",
            "wake_word": "magpie",
            "wake_heard": True,
            "command": "find M4 T-nuts",
            "route": "/assistant/chat",
            "query": "find M4 T-nuts",
            "audio_threshold": 800,
            "runtime_version": current_runtime,
            "backend_response": {
                "ok": True,
                    "interaction_id": interaction_id,
                "automation": {"lit": True},
            },
        },
    )
    client.post(
        "/screen/heartbeat",
        json={
            "screen_id": "cabinet-kiosk",
            "url": "http://127.0.0.1:8765/",
            "build_label": "test",
            "last_seen_interaction_id": interaction_id,
            "status": "active",
        },
    )
    for error in ("no_transcript", "awaiting_command_silence"):
        client.post(
            "/voice/attempts",
            json={
                "rms": 1100,
                "activity_rms": 1200,
                "wake_word": "magpie",
                "wake_heard": True,
                "audio_threshold": 800,
                "runtime_version": current_runtime,
                "error": error,
            },
        )

    readiness = client.get("/demo/readiness").json()
    checks = {check["key"]: check for check in readiness["checks"]}

    assert checks["voice_find"]["passed"] is False
    assert readiness["voice_window"]["size"] == 3
    assert readiness["voice_window"]["passed"] == 1
    assert readiness["voice_window"]["rate"] == 0.333
    assert "latest 5 voice activations" in checks["voice_find"]["next_step"]


def test_workspace_readiness_tracks_visual_setup_and_first_putaway(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "empty",
                "columns": 1,
                "rows_bottom_to_top": [],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=0,
        backend="none",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    empty = client.get("/workspace/readiness").json()

    assert empty["schema"] == "magpie.workspace_readiness.v2"
    assert empty["ready"] is False
    assert empty["stage"] == "needs_storage"
    assert empty["required_passed"] == 1
    assert empty["required_total"] == 3
    assert empty["recommended_passed"] == 0
    assert empty["counts"] == {
        "roots": 0,
        "locations": 0,
        "destinations": 0,
        "items": 0,
        "placements": 0,
        "ambiguous_name_groups": 0,
    }
    empty_checks = {check["key"]: check for check in empty["checks"]}
    assert empty_checks["storage_map"]["required"] is True
    assert empty_checks["storage_map"]["action"]["href"] == (
        "/storage?view=places&onboarding=1"
    )
    assert empty_checks["first_item"]["required"] is False

    client.post(
        "/locations",
        json={"id": "workshop", "type": "room", "label": "Workshop"},
    )
    needs_destination = client.get("/workspace/readiness").json()

    assert needs_destination["ready"] is False
    assert needs_destination["stage"] == "needs_destination"
    assert needs_destination["required_passed"] == 2
    assert needs_destination["counts"]["destinations"] == 0
    needs_destination_checks = {
        check["key"]: check for check in needs_destination["checks"]
    }
    assert needs_destination_checks["item_destination"]["action"]["href"] == (
        "/storage?view=places&onboarding=destination"
    )

    client.post(
        "/locations",
        json={
            "id": "small-parts",
            "type": "bin",
            "label": "Small Parts",
            "parent_id": "workshop",
        },
    )
    storage_ready = client.get("/workspace/readiness").json()

    assert storage_ready["ready"] is True
    assert storage_ready["stage"] == "storage_ready"
    assert storage_ready["required_passed"] == 3
    assert storage_ready["recommended_passed"] == 0
    assert storage_ready["summary"].startswith("Your storage is ready.")

    client.post(
        "/inventory",
        json={"name": "M4 T-nuts", "bin_id": "small-parts"},
    )
    started = client.get("/workspace/readiness").json()

    assert started["ready"] is True
    assert started["stage"] == "inventory_started"
    assert started["recommended_passed"] == 1
    assert started["counts"]["items"] == 1
    assert started["counts"]["placements"] == 1
    assert started["summary"] == "Magpie is ready for find and put-away."


def test_workspace_readiness_blocks_ambiguous_legacy_place_paths(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "empty",
                "columns": 1,
                "rows_bottom_to_top": [],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    repositories = StorageRepositories.sqlite(database_path)
    repositories.locations.upsert(
        Location(
            id="main-cabinet",
            type="cabinet",
            label="Main Cabinet",
            accepts_items=True,
        )
    )
    _insert_legacy_location(
        database_path,
        location_id="main-cabinet-2",
        location_type="cabinet",
        label=" main   cabinet ",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(database_path),
            ),
            repositories=repositories,
        )
    )

    readiness = client.get("/workspace/readiness").json()
    checks = {check["key"]: check for check in readiness["checks"]}

    assert readiness["ready"] is False
    assert readiness["stage"] == "needs_path_repair"
    assert readiness["summary"] == (
        "Resolve duplicate place names before Magpie uses these storage paths."
    )
    assert readiness["counts"]["ambiguous_name_groups"] == 1
    assert checks["storage_map"]["passed"] is True
    assert checks["item_destination"]["passed"] is True
    assert checks["unambiguous_paths"]["passed"] is False
    assert checks["unambiguous_paths"]["required"] is True
    assert checks["unambiguous_paths"]["action"] == {
        "label": "Review duplicates",
        "href": "/storage?view=places&conflicts=1",
    }


def test_product_readiness_includes_mobile_and_shared_storage_gates(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
        zebra=ZebraConfig(base_url="http://magpie.local:8765"),
    )
    client = TestClient(create_app(cfg))
    client.post(
        "/locations",
        json={"id": "attic", "type": "room", "label": "Attic"},
    )

    readiness = client.get("/product/readiness").json()

    assert readiness["schema"] == "magpie.product_readiness.v8"
    assert readiness["ready"] is False
    assert readiness["storage"] == {"backend": "sqlite", "workspace_id": "local"}
    assert readiness["public_origin"] == "http://magpie.local:8765"
    checks = {check["key"]: check for check in readiness["checks"]}
    assert checks["agent_control_plane"]["passed"] is False
    assert checks["mobile_https_origin"]["passed"] is False
    assert checks["shared_storage"]["passed"] is False
    assert checks["central_service"]["passed"] is False
    assert checks["mobile_pwa"]["passed"] is True
    for check in readiness["checks"]:
        if check["passed"]:
            assert check["next_step"] == "", check["key"]
            assert "action" not in check, check["key"]
    assert checks["secure_edge_controller"]["passed"] is False
    assert checks["whole_storage_agent"]["passed"] is True
    assert checks["physical_storage_integrity"]["passed"] is True
    assert checks["agent_storage_journeys"]["passed"] is False
    assert checks["agent_correction_journey"]["passed"] is False
    assert checks["workspace_management_journey"]["passed"] is False
    assert readiness["required_total"] == 17
    assert readiness["passed"] == sum(
        1 for check in readiness["checks"] if check["required"] and check["passed"]
    )
    assert readiness["failed"] == sum(
        1 for check in readiness["checks"] if check["required"] and not check["passed"]
    )
    assert readiness["all_passed"] + readiness["all_failed"] == len(
        readiness["checks"]
    )
    assert checks["voice_find"]["group"] == "hardware"
    assert checks["voice_find"]["required"] is False
    assert checks["cabinet_hardware"]["required"] is False
    assert checks["secure_edge_controller"]["required"] is False
    assert checks["secure_edge_voice"]["required"] is False
    assert checks["secure_edge_screen"]["required"] is False
    assert checks["secure_edge_printer"]["required"] is False
    assert checks["secure_edge_check_station"]["required"] is False
    assert checks["whole_storage_agent"]["group"] == "workspace"
    assert checks["whole_storage_agent"]["required"] is True
    assert checks["physical_storage_integrity"]["group"] == "workspace"
    assert checks["physical_storage_integrity"]["required"] is True
    assert checks["agent_storage_journeys"]["required"] is True
    assert checks["agent_correction_journey"]["required"] is True
    assert checks["workspace_management_journey"]["required"] is True
    groups = {group["key"]: group for group in readiness["groups"]}
    assert groups["software"]["required"] is True
    assert groups["software"]["total"] == 12
    assert groups["workspace"]["required"] is True
    assert groups["workspace"]["total"] == 2
    assert groups["hardware"]["required"] is False
    assert groups["validation"]["required"] is True
    assert groups["validation"]["total"] == 3
    assert "storage-agent provider" in checks["agent_control_plane"]["next_step"]
    assert "public HTTPS origin" in checks["mobile_https_origin"]["next_step"]
    assert "migrate the live workspace" in checks["shared_storage"]["next_step"]
    assert "Hardware-neutral" in checks["central_service"]["label"]
    assert "commands-scoped cabinet token" in checks["secure_edge_controller"]["next_step"]


def test_product_readiness_requires_workspace_routed_central_service(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "configured")
    client = _hosted_config_client(
        tmp_path,
        edge_mode="local",
        reported_storage_backend="supabase",
    )

    readiness = client.get("/product/readiness").json()
    central = next(
        check for check in readiness["checks"] if check["key"] == "central_service"
    )

    assert central["passed"] is False
    assert "runtime=standalone_service" in central["evidence"]
    assert "edge=local" in central["evidence"]
    assert "workspace-routed edge commands" in central["next_step"]

    routed = _hosted_config_client(
        tmp_path,
        reported_storage_backend="supabase",
    ).get("/product/readiness").json()
    routed_central = next(
        check for check in routed["checks"] if check["key"] == "central_service"
    )
    assert routed_central["passed"] is True
    assert "runtime=central_service" in routed_central["evidence"]
    assert "edge=workspace_queue" in routed_central["evidence"]


def test_product_readiness_rejects_ambiguous_storage_paths(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(database_path),
            )
        )
    )
    location_store = LocationStore(database_path)
    location_store.upsert(
        Location(id="attic", type="room", label="Attic", accepts_items=True)
    )
    _insert_legacy_location(
        database_path,
        location_id="attic-copy",
        location_type="room",
        label=" attic ",
    )

    readiness = client.get("/product/readiness").json()
    check = next(
        row for row in readiness["checks"] if row["key"] == "whole_storage_agent"
    )

    assert check["passed"] is False
    assert "ambiguous_names=1" in check["evidence"]
    assert check["next_step"] == (
        "Review 1 duplicate place-name group in Places and rename, move, or "
        "archive one place in each group."
    )
    assert check["action"] == {
        "label": "Review duplicate places",
        "href": "/storage?view=places&conflicts=1",
    }


def test_rule_management_surfaces_and_blocks_unavailable_targets(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(database_path),
            )
        )
    )
    client.post(
        "/locations",
        json={"id": "workshop", "type": "room", "label": "Workshop"},
    )
    client.post(
        "/locations",
        json={"id": "garage", "type": "room", "label": "Garage"},
    )
    client.post(
        "/locations",
        json={"id": "attic", "type": "room", "label": "Attic"},
    )
    store = InventoryStore(database_path)
    archived_rule_id = store.learn_rule(
        rule_text="Holiday decorations belong in the attic.",
        trigger_terms=["holiday", "decorations"],
        target_location_id="attic",
    )
    assert client.post("/locations/attic/archive").status_code == 200

    archived_rule = next(
        rule
        for rule in client.get("/rules").json()["placement_rules"]
        if rule["id"] == archived_rule_id
    )
    assert archived_rule["target_active"] is False
    assert archived_rule["target_archived"] is True
    assert archived_rule["target_missing"] is False
    assert archived_rule["target_issue"] == "archived_target"

    readiness = client.get("/product/readiness").json()
    check = next(
        row for row in readiness["checks"] if row["key"] == "whole_storage_agent"
    )
    assert check["passed"] is False
    assert "unavailable_rule_targets=1" in check["evidence"]
    assert check["next_step"] == (
        "Repair or disable 1 active placement rule whose target is unavailable."
    )
    assert check["action"] == {
        "label": "Review rule issues",
        "href": "/storage?view=rules&scope=issues",
    }

    disabled = client.post(f"/rules/placement/{archived_rule_id}/disable")
    assert disabled.status_code == 200
    restore_archived = client.post(
        f"/rules/placement/{archived_rule_id}/restore"
    )
    assert restore_archived.status_code == 400
    assert restore_archived.json()["detail"] == (
        "choose an active target location before restoring this rule"
    )

    missing_rule_id = store.learn_rule(
        rule_text="Camping fuel belongs on the removed shelf.",
        trigger_terms=["camping", "fuel"],
        target_location_id="removed-shelf",
    )
    missing_rule = next(
        rule
        for rule in client.get("/rules").json()["placement_rules"]
        if rule["id"] == missing_rule_id
    )
    assert missing_rule["target_active"] is False
    assert missing_rule["target_archived"] is False
    assert missing_rule["target_missing"] is True
    assert missing_rule["target_issue"] == "missing_target"

    assert client.post(f"/rules/placement/{missing_rule_id}/disable").status_code == 200
    repaired = client.get("/product/readiness").json()
    repaired_check = next(
        row for row in repaired["checks"] if row["key"] == "whole_storage_agent"
    )
    assert repaired_check["passed"] is True
    assert "unavailable_rule_targets=0" in repaired_check["evidence"]


def test_product_readiness_requires_legacy_placement_issues_to_be_repaired(
    tmp_path,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(database_path),
            )
        )
    )
    attic = client.post(
        "/locations",
        json={"id": "attic", "type": "room", "label": "Attic"},
    ).json()["id"]
    workshop = client.post(
        "/locations",
        json={"id": "workshop", "type": "room", "label": "Workshop"},
    ).json()["id"]
    destination = client.post(
        "/locations",
        json={
            "id": "workshop-tote",
            "type": "tote",
            "label": "Workshop Tote",
            "parent_id": workshop,
        },
    ).json()["id"]
    store = InventoryStore(database_path)
    item_id = store.upsert_item(name="Heirloom jewelry")
    store.put_inventory(
        item_id=item_id,
        bin_id=attic,
        quantity=1,
        unit="box",
        normalize_bin=False,
    )

    readiness = client.get("/product/readiness").json()
    check = next(
        row for row in readiness["checks"] if row["key"] == "whole_storage_agent"
    )

    assert check["passed"] is False
    assert "organizer_only=1" in check["evidence"]
    assert check["next_step"] == (
        "Move 1 inventory placement out of organizer-only places and into "
        "item destinations."
    )
    assert check["action"] == {
        "label": "Review placement issues",
        "href": "/storage?view=inventory&scope=placement",
    }

    assert client.post(
        "/inventory/move",
        json={"item_id": item_id, "from_bin": attic, "to_bin": destination},
    ).status_code == 200
    repaired = client.get("/product/readiness").json()
    repaired_check = next(
        row for row in repaired["checks"] if row["key"] == "whole_storage_agent"
    )
    assert repaired_check["passed"] is True
    assert "organizer_only=0" in repaired_check["evidence"]

    orphan_id = store.upsert_item(name="Orphaned camera cable")
    store.put_inventory(
        item_id=orphan_id,
        bin_id="removed-shelf",
        quantity=1,
        unit="each",
        normalize_bin=False,
    )
    orphaned = client.get("/product/readiness").json()
    orphaned_check = next(
        row for row in orphaned["checks"] if row["key"] == "whole_storage_agent"
    )
    assert orphaned_check["passed"] is False
    assert "orphaned=1" in orphaned_check["evidence"]
    assert orphaned_check["action"] == {
        "label": "Review placement issues",
        "href": "/storage?view=inventory&scope=placement",
    }

    assert client.post(
        "/inventory/move",
        json={
            "item_id": orphan_id,
            "from_bin": "removed-shelf",
            "to_bin": destination,
        },
    ).status_code == 200
    final = client.get("/product/readiness").json()
    final_check = next(
        row for row in final["checks"] if row["key"] == "whole_storage_agent"
    )
    assert final_check["passed"] is True
    assert "orphaned=0" in final_check["evidence"]

    assert client.post(f"/locations/{destination}/archive").status_code == 200
    archived = client.get("/product/readiness").json()
    archived_check = next(
        row for row in archived["checks"] if row["key"] == "whole_storage_agent"
    )
    assert archived_check["passed"] is True
    assert "orphaned=0" in archived_check["evidence"]


def test_product_readiness_names_only_the_missing_whole_storage_requirements(
    tmp_path,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    client.post(
        "/locations",
        json={"id": "workshop", "type": "room", "label": "Workshop"},
    )

    readiness = client.get("/product/readiness").json()
    check = next(
        row for row in readiness["checks"] if row["key"] == "whole_storage_agent"
    )

    assert check["passed"] is False
    assert check["next_step"] == "Create another top-level storage space in Places."
    assert check["action"] == {
        "label": "Add storage space",
        "href": "/storage?view=places&onboarding=root",
    }
    assert "agent" not in check["next_step"].lower()
    assert "duplicate" not in check["next_step"].lower()


def test_product_readiness_counts_visual_setup_root_types(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    for root_name, root_type in (
        ("My Home", "home"),
        ("Storage Unit A", "storage_unit"),
    ):
        response = client.post(
            "/storage/setup",
            json={
                "root_name": root_name,
                "root_type": root_type,
                "children": [],
            },
        )
        assert response.status_code == 200

    readiness = client.get("/product/readiness").json()
    check = next(
        row for row in readiness["checks"] if row["key"] == "whole_storage_agent"
    )

    assert check["passed"] is True
    assert "roots=My Home, Storage Unit A" in check["evidence"]


def test_visual_storage_setup_rejects_destination_sibling_name_collision(
    tmp_path,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )

    response = client.post(
        "/storage/setup",
        json={
            "root_name": "Garage",
            "root_type": "room",
            "children": [{"label": "General storage", "type": "area"}],
            "destination": {
                "label": " general STORAGE ",
                "type": "bin",
                "capacity_class": "medium",
            },
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "first container name conflicts with a starter place at the same level"
    )
    assert client.get("/locations").json()["roots"] == []


def test_product_readiness_requires_a_recorded_workspace_management_journey(
    tmp_path,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
                assistant_backend="deterministic",
            )
        )
    )
    initial_readiness = client.get("/product/readiness").json()
    initial_check = next(
        row
        for row in initial_readiness["checks"]
        if row["key"] == "workspace_management_journey"
    )
    assert initial_check["action"] == {
        "label": "Set up storage space",
        "href": "/storage?view=places&onboarding=1",
    }

    setups = []
    for root_name, container_name in (
        ("Garage", "Tool box"),
        ("Attic", "Parts box"),
    ):
        response = client.post(
            "/storage/setup",
            json={
                "root_name": root_name,
                "root_type": "room",
                "children": [],
                "destination": {
                    "label": container_name,
                    "type": "box",
                    "capacity_class": "medium",
                },
            },
        )
        assert response.status_code == 200
        setups.append(response.json())

    setup_readiness = client.get("/product/readiness").json()
    setup_check = next(
        row
        for row in setup_readiness["checks"]
        if row["key"] == "workspace_management_journey"
    )
    assert setup_check["action"] == {
        "label": "Move inventory",
        "href": "/storage?view=inventory",
    }

    source_id = setups[0]["destination"]["id"]
    destination_id = setups[1]["destination"]["id"]
    stored = client.post(
        f"/locations/{source_id}/quick-put",
        json={"text": "socket adapter"},
    )
    assert stored.status_code == 200
    item_id = stored.json()["item"]["item_id"]
    moved = client.post(
        "/inventory/move",
        json={
            "item_id": item_id,
            "from_bin": source_id,
            "to_bin": destination_id,
        },
    )
    assert moved.status_code == 200

    moved_readiness = client.get("/product/readiness").json()
    moved_check = next(
        row
        for row in moved_readiness["checks"]
        if row["key"] == "workspace_management_journey"
    )
    assert moved_check["action"] == {
        "label": "Test place recovery",
        "href": "/storage?view=places",
    }

    lifecycle = client.post(
        "/locations",
        json={
            "label": "Temporary test bin",
            "type": "bin",
            "parent_id": setups[1]["root"]["id"],
        },
    )
    assert lifecycle.status_code == 200
    lifecycle_id = lifecycle.json()["id"]
    nested = client.post(
        "/locations",
        json={
            "label": "Temporary inner pouch",
            "type": "pouch",
            "parent_id": lifecycle_id,
        },
    )
    assert nested.status_code == 200
    nested_id = nested.json()["id"]
    assert client.post(f"/locations/{lifecycle_id}/archive").status_code == 200
    assert client.get(f"/locations/{lifecycle_id}/removal-preview").status_code == 404
    restore_preview = client.get(
        f"/locations/{lifecycle_id}/removal-preview?include_archived=true"
    )
    assert restore_preview.status_code == 200
    assert restore_preview.json()["location_count"] == 2
    assert set(restore_preview.json()["subtree_ids"]) == {lifecycle_id, nested_id}
    assert client.post(f"/locations/{lifecycle_id}/restore").status_code == 200

    readiness = client.get("/product/readiness").json()
    check = next(
        row
        for row in readiness["checks"]
        if row["key"] == "workspace_management_journey"
    )

    assert check["passed"] is True
    assert "action" not in check
    assert readiness["workspace_management_journey"] == {
        "visual_multi_root_setup": True,
        "cross_root_item_move": True,
        "location_archive_restore": True,
    }

    for index in range(125):
        noise = client.post(
            "/assistant/chat",
            json={"text": f"find readiness noise {index}", "mode": "find"},
        )
        assert noise.status_code == 200
    latest = client.get("/assistant/interactions?limit=100").json()["interactions"]
    assert len(latest) == 100
    assert all(row["route"] == "/assistant/chat" for row in latest)

    retained = client.get("/product/readiness").json()
    retained_check = next(
        row
        for row in retained["checks"]
        if row["key"] == "workspace_management_journey"
    )
    assert retained_check["passed"] is True
    assert retained["workspace_management_journey"] == {
        "visual_multi_root_setup": True,
        "cross_root_item_move": True,
        "location_archive_restore": True,
    }


def test_interaction_feeds_reconcile_quick_put_receipts_with_current_placements(
    tmp_path,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
                assistant_backend="deterministic",
            )
        )
    )
    source = client.post(
        "/locations",
        json={
            "label": "Source bin",
            "type": "bin",
            "accepts_items": True,
        },
    ).json()
    destination = client.post(
        "/locations",
        json={
            "label": "Destination bin",
            "type": "bin",
            "accepts_items": True,
        },
    ).json()
    stored = client.post(
        f"/locations/{source['id']}/quick-put",
        json={"text": "socket adapter"},
    ).json()
    moved = client.post(
        "/inventory/move",
        json={
            "item_id": stored["item_id"],
            "from_bin": source["id"],
            "to_bin": destination["id"],
        },
    )
    assert moved.status_code == 200

    assistant_feed = client.get("/assistant/interactions?limit=20").json()
    quick_put = next(
        interaction
        for interaction in assistant_feed["interactions"]
        if "quick-put" in interaction["route"]
    )
    assert quick_put["current_items"] == [
        {
            "item_id": stored["item_id"],
            "name": "Socket Adapter",
            "active": True,
            "placements": [
                {
                    "location_id": destination["id"],
                    "path": [
                        {
                            "id": destination["id"],
                            "label": "Destination bin",
                        }
                    ],
                    "path_label": "Destination bin",
                }
            ],
        }
    ]

    kiosk_feed = client.get(
        "/kiosk/state",
        params={
            "after_id": quick_put["id"] - 1,
            "wait_seconds": 0,
        },
    ).json()
    kiosk_quick_put = next(
        interaction
        for interaction in kiosk_feed["interactions"]
        if interaction["id"] == quick_put["id"]
    )
    assert kiosk_quick_put["current_items"] == quick_put["current_items"]

    archived = client.post(f"/inventory/{stored['item_id']}/archive")
    assert archived.status_code == 200
    archived_feed = client.get("/assistant/interactions?limit=20").json()
    archived_quick_put = next(
        interaction
        for interaction in archived_feed["interactions"]
        if interaction["id"] == quick_put["id"]
    )
    assert archived_quick_put["current_items"] == [
        {
            "item_id": stored["item_id"],
            "name": "Socket Adapter",
            "active": False,
            "placements": [],
        }
    ]


def test_product_readiness_accepts_configured_chat_completions_agent(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )

    readiness = client.get("/product/readiness").json()
    check = next(
        row for row in readiness["checks"] if row["key"] == "agent_control_plane"
    )

    assert check["passed"] is True
    assert "provider=openrouter" in check["evidence"]
    assert "api=chat_completions" in check["evidence"]


def test_product_readiness_retains_real_agent_storage_journeys(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(database_path),
    )
    client = TestClient(create_app(cfg))
    client.post(
        "/locations",
        json={
            "id": "printercab",
            "type": "cabinet",
            "label": "PrinterCab",
            "accepts_items": True,
        },
    )
    client.post(
        "/inventory",
        json={"name": "Dremel", "bin_id": "printercab"},
    )
    assert client.post(
        "/assistant/chat",
        json={"text": "List my storage spaces"},
    ).status_code == 200
    assert client.post(
        "/assistant/chat",
        json={"text": "Find Dremel"},
    ).status_code == 200
    assert client.post(
        "/assistant/chat",
        json={"text": "Preview labels for PrinterCab"},
    ).status_code == 200

    store = InventoryStore(database_path)
    for index in range(125):
        store.record_assistant_interaction(
            route="/assistant/chat",
            command=f"Noise {index}",
            response={"action": "answered", "message": "No milestone."},
        )

    readiness = client.get("/product/readiness").json()
    checks = {check["key"]: check for check in readiness["checks"]}

    assert checks["agent_storage_journeys"]["passed"] is True
    assert readiness["agent_storage_journeys"] == {
        "root_discovery": True,
        "cross_root_retrieval": True,
        "label_preview": True,
    }


def test_product_readiness_guides_each_agent_storage_journey_step(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="wled",
                wled_url="http://127.0.0.1:1",
                layout_path=str(layout_path),
                database_path=str(database_path),
            )
        )
    )
    client.post(
        "/locations",
        json={
            "id": "printercab",
            "type": "cabinet",
            "label": "Printer Cabinet",
            "accepts_items": True,
        },
    )
    client.post(
        "/inventory",
        json={"name": "Dremel attachments", "bin_id": "printercab"},
    )
    store = InventoryStore(database_path)

    initial = client.get("/product/readiness").json()
    initial_check = next(
        row for row in initial["checks"] if row["key"] == "agent_storage_journeys"
    )
    assert initial_check["action"] == {
        "label": "List storage spaces",
        "href": "/?prompt=List%20my%20storage%20spaces",
    }

    assert client.post(
        "/assistant/chat",
        json={"text": "List my storage spaces"},
    ).status_code == 200
    discovered = client.get("/product/readiness").json()
    discovered_check = next(
        row
        for row in discovered["checks"]
        if row["key"] == "agent_storage_journeys"
    )
    assert discovered_check["action"] == {
        "label": "Find Dremel attachments",
        "href": "/?action=find&prompt=Find%20Dremel%20attachments",
    }

    found = client.post(
        "/assistant/chat",
        json={"text": "Find Dremel attachments"},
    )
    assert found.status_code == 200
    retrieved = client.get("/product/readiness").json()
    retrieved_check = next(
        row for row in retrieved["checks"] if row["key"] == "agent_storage_journeys"
    )
    assert retrieved_check["action"] == {
        "label": "Preview Printer Cabinet labels",
        "href": "/?prompt=Preview%20labels%20for%20Printer%20Cabinet",
    }

    assert client.post(
        "/assistant/chat",
        json={"text": "Preview labels for Printer Cabinet"},
    ).status_code == 200
    complete = client.get("/product/readiness").json()
    complete_check = next(
        row for row in complete["checks"] if row["key"] == "agent_storage_journeys"
    )
    assert complete_check["passed"] is True
    assert "action" not in complete_check


def test_product_readiness_requires_explained_retained_agent_correction(
    tmp_path,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    database_path = tmp_path / "inventory.db"
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=2,
                backend="wled",
                wled_url="http://127.0.0.1:1",
                layout_path=str(layout_path),
                database_path=str(database_path),
            )
        )
    )
    store = InventoryStore(database_path)
    item_id = store.upsert_item(name="M4 T-nuts")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=2, unit="each")
    original_rule_id = store.learn_rule(
        rule_text="M4 T-nuts belong in B001.",
        trigger_terms=["M4", "T-nuts"],
        target_location_id="B001",
    )
    proposed = client.post(
        "/assistant/chat",
        json={"text": "put away two M4 T-nuts"},
    ).json()
    assert proposed["decision"]["confirmation"]["explanation"]["summary"]

    corrected = client.post(
        "/assistant/followup",
        json={
            "text": "actually put it in B002",
            "context": {
                "kind": "filed",
                "item": {"name": "M4 T-nuts"},
                "saved_item_id": item_id,
                "destination": {"id": "B001"},
            },
        },
    ).json()

    assert corrected["action"] == "learned_placement_correction"
    assert corrected["superseded_rule_ids"] == [original_rule_id]
    assert store.rule(original_rule_id) is None
    assert store.rule(original_rule_id, active_only=False) is not None
    readiness_before_reuse = client.get("/product/readiness").json()
    checks_before_reuse = {
        check["key"]: check for check in readiness_before_reuse["checks"]
    }
    assert checks_before_reuse["agent_correction_journey"]["passed"] is False
    assert readiness_before_reuse["agent_correction_journey"] == {
        "explained_proposal": True,
        "correction_applied": True,
        "correction_retained": False,
    }
    assert checks_before_reuse["agent_correction_journey"]["action"] == {
        "label": "Verify learned correction",
        "href": "/?action=put-away&prompt=Put%20away%20M4%20T-nuts",
    }

    reused = client.post(
        "/assistant/chat",
        json={"text": "put away M4 T-nuts"},
    ).json()
    assert reused["decision"]["destination"]["id"] == "B002"
    assert reused["decision"]["learning"]["rule_adherence"] == [
        {
            "rule_id": corrected["rule_id"],
            "rule_type": "placement",
            "rule_text": "M4 T-nuts belongs in Test › B002.",
            "expected": "B002",
            "actual": "B002",
            "followed": True,
        }
    ]

    readiness = client.get("/product/readiness").json()
    checks = {check["key"]: check for check in readiness["checks"]}
    assert checks["agent_correction_journey"]["passed"] is True
    assert "action" not in checks["agent_correction_journey"]
    assert readiness["agent_correction_journey"] == {
        "explained_proposal": True,
        "correction_applied": True,
        "correction_retained": True,
    }

    for index in range(125):
        store.record_assistant_interaction(
            route="/assistant/chat",
            command=f"Later conversation {index}",
            response={"action": "answered", "message": "No correction evidence."},
        )
    retained = client.get("/product/readiness").json()
    assert retained["agent_correction_journey"] == {
        "explained_proposal": True,
        "correction_applied": True,
        "correction_retained": True,
    }


def test_product_readiness_does_not_scan_assistant_history(
    tmp_path, monkeypatch
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="wled",
                wled_url="http://127.0.0.1:1",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    monkeypatch.setattr(
        InventoryStore,
        "assistant_interactions_after",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("readiness must not scan assistant history")
        ),
    )

    response = client.get("/product/readiness")

    assert response.status_code == 200


def test_whole_storage_fast_paths_avoid_the_planning_model(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "searchable_storage_led.api.run_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("whole-storage fast paths should not call the planning model")
        ),
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )
    client.post(
        "/locations",
        json={
            "id": "printercab",
            "type": "cabinet",
            "label": "PrinterCab",
            "accepts_items": True,
        },
    )
    client.post(
        "/locations",
        json={
            "id": "attic",
            "type": "room",
            "label": "Attic",
        },
    )
    client.post(
        "/inventory",
        json={"name": "Dremel attachments", "bin_id": "printercab"},
    )

    storage = client.post(
        "/assistant/chat",
        json={"text": "Show me all my storage"},
    ).json()
    found = client.post(
        "/assistant/chat",
        json={"text": "Find Dremel attachments"},
    ).json()
    labels = client.post(
        "/assistant/chat",
        json={"text": "Print labels for PrinterCab"},
    ).json()
    readiness = client.get("/product/readiness").json()

    assert {root["id"] for root in storage["trace"][0]["result"]} == {
        "attic",
        "printercab",
    }
    assert found["raw"]["results"][0]["path"][0]["id"] == "printercab"
    assert labels["label_preview"]["found"] is True
    assert labels["label_preview"]["location_id"] == "printercab"
    assert labels["label_preview"]["url"] == "/locations/printercab/labels.html"
    assert readiness["agent_storage_journeys"]["cross_root_retrieval"] is True


def test_multi_destination_confirm_does_not_attach_mixed_photo_to_one_bin(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    photo_id = client.post(
        "/photos",
        json={
            "filename": "mixed-parts.jpg",
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(b"image").decode("ascii"),
        },
    ).json()["photo"]["id"]

    response = client.post(
        "/assistant/confirm",
        json={
            "command": "sort this photo into separate bins",
            "photo_ids": [photo_id],
            "plan": [
                {"tool": "put_item", "arguments": {"item": "tactile switches", "location_id": "B001", "quantity": 6}},
                {"tool": "put_item", "arguments": {"item": "LEDs", "location_id": "B002", "quantity": 10}},
            ],
        },
    )

    assert response.status_code == 200
    applied = response.json()["applied"]
    assert [(entry["stored"], entry["location_id"]) for entry in applied] == [
        ("tactile switches", "B001"),
        ("LEDs", "B002"),
    ]
    assert all(entry["item_id"] for entry in applied)
    assert response.json()["needs_review"] is True
    assert response.json()["review_item_ids"] == [entry["item_id"] for entry in applied]
    assert response.json()["photo_attachments"] == {
        "photo_ids": [photo_id],
        "item_links": [
            {"photo_id": photo_id, "item_id": applied[0]["item_id"]},
            {"photo_id": photo_id, "item_id": applied[1]["item_id"]},
        ],
        "location_links": [],
        "review_links": [
            {"photo_id": photo_id, "item_id": applied[0]["item_id"], "location_id": "B001"},
            {"photo_id": photo_id, "item_id": applied[1]["item_id"], "location_id": "B002"},
        ],
    }
    assert client.get("/locations/B001").json()["location"]["photos"] == []
    assert client.get("/locations/B002").json()["location"]["photos"] == []
    review_items = sorted(
        client.get("/inventory/review").json()["items"],
        key=lambda item: item["bin_id"],
    )
    assert [(item["name"], item["bin_id"], item["photos"]) for item in review_items] == [
        ("tactile switches", "B001", [f"/photos/{photo_id}"]),
        ("LEDs", "B002", [f"/photos/{photo_id}"]),
    ]
    assert {item["review_reason"] for item in review_items} == {
        "Photo sort created multiple item groups; confirm each item and destination."
    }
    assert client.get("/locations/B001").json()["items"][0]["photos"] == [f"/photos/{photo_id}"]
    assert client.get("/locations/B002").json()["items"][0]["photos"] == [f"/photos/{photo_id}"]


def test_confirmed_put_item_review_flag_selectively_marks_review_queue(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/confirm",
        json={
            "command": "put away these bench parts",
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "known spacer",
                        "location_id": "B001",
                    },
                },
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "unknown bracket",
                        "location_id": "B002",
                        "needs_review": True,
                    },
                },
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["needs_review"] is True
    assert body["review_item_ids"] == [body["applied"][1]["item_id"]]
    review_items = client.get("/inventory/review").json()["items"]
    assert [(item["name"], item["bin_id"]) for item in review_items] == [
        ("unknown bracket", "B002")
    ]
    assert review_items[0]["review_reason"] == "Agent marked this item for review."


def test_single_destination_confirm_attaches_photo_to_item_and_location(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    db_path = tmp_path / "inventory.db"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    photo_id = client.post(
        "/photos",
        json={
            "filename": "pry-tools.jpg",
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(b"image").decode("ascii"),
        },
    ).json()["photo"]["id"]

    response = client.post(
        "/assistant/confirm",
        json={
            "command": "put away blue pry tools",
            "photo_ids": [photo_id],
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "Blue pry tools",
                        "location_id": "B001",
                        "quantity": 3,
                        "unit": "pieces",
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    item_id = body["applied"][0]["item_id"]
    assert body["photo_attachments"] == {
        "photo_ids": [photo_id],
        "item_links": [{"photo_id": photo_id, "item_id": item_id}],
        "location_links": [],
        "review_links": [],
    }
    assert body["receipt"] == {
        "schema": "magpie.confirmation_receipt.v1",
        "change_count": 1,
        "focus_location_id": "B001",
        "summary": "Stored Blue pry tools in Test › B001.",
        "photos": {"uploaded": 1, "attached": 1, "pending": 0},
        "needs_review": False,
        "undo_available": True,
    }
    with InventoryStore(db_path).connect() as conn:
        row = conn.execute(
            "SELECT item_id, location_id FROM photos WHERE id = ?",
            (photo_id,),
        ).fetchone()
    assert row["item_id"] == item_id
    assert row["location_id"] is None
    contents = client.get("/locations/B001").json()["contents"]
    assert contents[0]["photos"] == [f"/photos/{photo_id}"]
    assert client.get("/locations/B001").json()["location"]["photos"] == []


def test_confirm_preserves_all_proposal_photos_when_client_sends_partial_list(
    tmp_path,
) -> None:
    layout_path = tmp_path / "layout.json"
    db_path = tmp_path / "inventory.db"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="wled",
                wled_url="http://127.0.0.1:1",
                layout_path=str(layout_path),
                database_path=str(db_path),
            )
        )
    )
    photo_ids = [
        client.post(
            "/photos",
            json={
                "filename": filename,
                "content_type": "image/jpeg",
                "data_base64": base64.b64encode(payload).decode("ascii"),
            },
        ).json()["photo"]["id"]
        for filename, payload in (("front.jpg", b"front"), ("side.jpg", b"side"))
    ]
    plan = [
        {
            "tool": "put_item",
            "arguments": {"item": "Blue pry tool", "location_id": "B001"},
        }
    ]
    store = InventoryStore(db_path)
    source_interaction_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="put away this blue pry tool",
        photo_ids=photo_ids,
        response={"plan": plan},
        status="pending",
    )

    response = client.post(
        "/assistant/confirm",
        json={
            "command": "put away this blue pry tool",
            "photo_ids": [photo_ids[1]],
            "source_interaction_id": source_interaction_id,
            "plan": plan,
        },
    )

    assert response.status_code == 200
    body = response.json()
    item_id = body["applied"][0]["item_id"]
    assert body["photo_attachments"]["photo_ids"] == photo_ids
    assert body["receipt"]["photos"] == {
        "uploaded": 2,
        "attached": 2,
        "pending": 0,
    }
    item = next(
        row
        for row in client.get("/inventory").json()["items"]
        if row["item_id"] == item_id
    )
    assert item["photos"] == [f"/photos/{photo_id}" for photo_id in photo_ids]
    confirmation = InventoryStore(db_path).assistant_interaction(
        body["confirmation_interaction_id"]
    )
    assert confirmation["photo_ids"] == photo_ids


def test_photo_attachment_failure_is_retried_without_reapplying_inventory(
    tmp_path,
    monkeypatch,
) -> None:
    layout_path = tmp_path / "layout.json"
    db_path = tmp_path / "inventory.db"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    photo_id = client.post(
        "/photos",
        json={
            "filename": "retry.jpg",
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(b"image").decode("ascii"),
        },
    ).json()["photo"]["id"]
    real_attach = InventoryStore.attach_photo
    failing = True
    attempts = 0

    def controlled_attach(self, *, item_id, photo_id):
        nonlocal attempts
        attempts += 1
        if failing:
            raise OSError("temporary photo link failure")
        return real_attach(self, item_id=item_id, photo_id=photo_id)

    monkeypatch.setattr(InventoryStore, "attach_photo", controlled_attach)
    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "put away retry part",
            "photo_ids": [photo_id],
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "Retry part",
                        "location_id": "B001",
                        "quantity": 3,
                    },
                }
            ],
        },
    ).json()

    assert attempts == 3
    assert confirmed["photo_attachments"]["pending"][0]["photo_id"] == photo_id
    assert confirmed["receipt"]["photos"] == {
        "uploaded": 1,
        "attached": 0,
        "pending": 1,
    }
    retry_request = confirmed["photo_attachments"]["retry_request"]
    assert client.get("/inventory").json()["items"][0]["quantity"] == 3

    failing = False
    retried = client.post("/photos/attach-retry", json=retry_request).json()

    assert retried["ok"] is True
    assert retried["photo_attachments"]["pending"] == []
    assert client.get("/inventory").json()["items"][0]["quantity"] == 3
    assert client.get("/inventory").json()["items"][0]["photos"] == [
        f"/photos/{photo_id}"
    ]


def test_confirming_edited_photo_sort_records_tuning_example(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    db_path = tmp_path / "inventory.db"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    photo_id = client.post(
        "/photos",
        json={
            "filename": "mixed-parts.jpg",
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(b"image").decode("ascii"),
        },
    ).json()["photo"]["id"]
    original_plan = [
        {"tool": "put_item", "arguments": {"item": "nuts", "location_id": "B001", "quantity": 2}},
        {"tool": "put_item", "arguments": {"item": "switches", "location_id": "B002", "quantity": 4}},
    ]
    store = InventoryStore(db_path)
    interaction_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="sort this photo into separate bins",
        photo_ids=[photo_id],
        response={"plan": original_plan},
        status="pending",
    )
    corrected_plan = [
        {"tool": "put_item", "arguments": {"item": "M4 T-nuts", "location_id": "B001", "quantity": 2}},
        {"tool": "put_item", "arguments": {"item": "tactile switches", "location_id": "B002", "quantity": 4}},
    ]

    response = client.post(
        "/assistant/confirm",
        json={
            "command": "sort this photo into separate bins",
            "photo_ids": [photo_id],
            "source_interaction_id": interaction_id,
            "plan": corrected_plan,
        },
    )

    assert response.status_code == 200
    example = InventoryStore(db_path).recent_examples(limit=1)[0]
    assert example["kind"] == "edited_photo_sort"
    payload = json.loads(example["action"])
    assert payload["original_plan"] == original_plan
    assert payload["corrected_plan"] == corrected_plan
    assert payload["photo_ids"] == [photo_id]


def test_canceling_photo_sort_records_rejected_tuning_example(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    db_path = tmp_path / "inventory.db"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    photo_id = client.post(
        "/photos",
        json={
            "filename": "mixed-parts.jpg",
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(b"image").decode("ascii"),
        },
    ).json()["photo"]["id"]
    rejected_plan = [
        {"tool": "put_item", "arguments": {"item": "assorted hardware", "location_id": "B001", "quantity": 1}},
        {"tool": "put_item", "arguments": {"item": "assorted electronics", "location_id": "B002", "quantity": 1}},
    ]
    store = InventoryStore(db_path)
    interaction_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="sort this photo into separate bins",
        photo_ids=[photo_id],
        response={"plan": rejected_plan},
        status="pending",
    )

    response = client.post(
        "/assistant/cancel",
        json={
            "command": "sort this photo into separate bins",
            "photo_ids": [photo_id],
            "source_interaction_id": interaction_id,
        },
    )

    assert response.status_code == 200
    cancellation = response.json()
    assert cancellation["cancellation_interaction_id"] > interaction_id
    fresh_store = InventoryStore(db_path)
    assert fresh_store.assistant_interaction(interaction_id)["status"] == "canceled"
    assert fresh_store.assistant_interaction(cancellation["cancellation_interaction_id"])[
        "response"
    ]["cancellation_interaction_id"] == cancellation["cancellation_interaction_id"]
    example = fresh_store.recent_examples(limit=1)[0]
    assert example["kind"] == "rejected_photo_sort"
    payload = json.loads(example["action"])
    assert payload["rejected_plan"] == rejected_plan
    assert payload["photo_ids"] == [photo_id]


def test_assistant_status_reports_effective_backend(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    response = client.get("/assistant/status")

    assert response.status_code == 200
    assert response.json()["configured_backend"] == "auto"
    assert response.json()["effective_backend"] == "deterministic"
    assert response.json()["openai_key_present"] is False
    assert response.json()["agent_ready"] is False
    assert response.json()["ready"] is True
    assert response.json()["max_steps"] == 4


def test_agent_uses_configured_model_turn_budget(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_agent(*args, **kwargs):
        captured.update(kwargs)
        return AgentResult(message="Ready.")

    monkeypatch.setattr("searchable_storage_led.api.run_agent", fake_run_agent)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
                agent_max_steps=3,
            )
        )
    )

    response = client.post(
        "/assistant/chat", json={"text": "move all wrenches into a large bin"}
    )

    assert response.status_code == 200
    assert captured["max_steps"] == 3


def test_voice_agent_mode_receives_only_daily_work_tools(
    tmp_path, monkeypatch
) -> None:
    captured: list[dict[str, object]] = []

    def fake_run_agent(*args, **kwargs):
        captured.append(kwargs)
        return AgentResult(message="Ready.")

    monkeypatch.setattr("searchable_storage_led.api.run_agent", fake_run_agent)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )

    voice = client.post(
        "/assistant/chat",
        json={"text": "put away M4 screws", "mode": "agent", "source": "voice"},
    )
    typed = client.post(
        "/assistant/chat",
        json={"text": "put away M4 screws", "mode": "agent", "source": "ui"},
    )

    assert voice.status_code == 200
    assert typed.status_code == 200
    assert captured[0]["allowed_tools"] == frozenset(
        {
            "attach_photo",
            "clear_lights",
            "explain_location_choice",
            "get_location",
            "light_location",
            "list_locations",
            "move_item",
            "put_item",
            "search_items",
            "suggest_locations",
        }
    )
    assert captured[1]["allowed_tools"] is None


def test_assistant_status_reports_openai_auto_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )

    status = client.get("/assistant/status").json()

    assert status["configured_backend"] == "auto"
    assert status["effective_backend"] == "openai"
    assert status["provider"] == "openai"
    assert status["agent_api"] == "responses"
    assert status["agent_ready"] is True


def test_assistant_status_accepts_ready_compatible_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account-123")
    monkeypatch.setenv("CLOUDFLARE_AI_API_TOKEN", "workers-ai")
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=0,
        backend="none",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
        assistant_backend="openai",
        llm=LlmConfig(
            fallbacks=(
                LlmEndpoint(
                    "https://api.example/accounts/${CLOUDFLARE_ACCOUNT_ID}/ai/v1",
                    "workers-model",
                    "CLOUDFLARE_AI_API_TOKEN",
                    "cloudflare",
                ),
            )
        ),
    )
    client = TestClient(create_app(cfg))

    status = client.get("/assistant/status").json()

    assert status["ready"] is True
    assert status["agent_ready"] is True
    assert status["effective_backend"] == "cloudflare"
    assert status["provider"] == "cloudflare"
    assert status["model"] == "workers-model"
    assert "account-123" in status["base_url"]


def test_favicon_is_noop_not_404(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    response = client.get("/favicon.ico")

    assert response.status_code == 204


def test_hardware_binding_rejects_unwired_and_malformed_pixels(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=3,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    valid = client.post(
        "/locations/B001/hardware-bindings",
        json={
            "kind": "wled-pixels",
            "controller_id": "main-cabinet",
            "address": "2, 1, 2",
        },
    )
    assert valid.status_code == 200
    assert valid.json()["binding"]["kind"] == "wled_pixels"
    assert valid.json()["binding"]["metadata"]["pixels"] == [1, 2]

    out_of_range = client.post(
        "/locations/B001/hardware-bindings",
        json={"kind": "wled_pixels", "address": "3"},
    )
    disconnected_out_of_range = client.post(
        "/locations/B001/hardware-bindings",
        json={"kind": "wled_pixels", "address": "3", "active": False},
    )
    malformed = client.post(
        "/locations/B001/hardware-bindings",
        json={"kind": "wled_pixels", "address": "one"},
    )
    empty = client.post(
        "/locations/B001/hardware-bindings",
        json={"kind": "wled_pixels", "address": ""},
    )

    assert out_of_range.status_code == 400
    assert "configured range 0..2" in out_of_range.json()["detail"]
    assert disconnected_out_of_range.status_code == 200
    assert malformed.status_code == 400
    assert "comma-separated integers" in malformed.json()["detail"]
    assert empty.status_code == 400
    assert "needs at least one pixel" in empty.json()["detail"]
    bindings = client.get("/locations/B001/hardware-bindings").json()["bindings"]
    assert len(bindings) == 3
    active = [binding for binding in bindings if binding["active"]]
    assert len(active) == 1
    assert active[0]["metadata"]["pixels"] == [1, 2]


def test_web_app_manifest_and_icon_are_served(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    manifest = client.get("/manifest.webmanifest")
    mobile = client.get("/mobile")
    icon = client.get("/app-icon.svg")
    theme = client.get("/theme.css")
    icon_192 = client.get("/app-icon-192.png")
    icon_512 = client.get("/app-icon-512.png")
    apple_icon = client.get("/apple-touch-icon.png")
    qr_decoder = client.get("/jsqr.min.js")
    photo_utils = client.get("/photo-utils.js")
    location_display = client.get("/location-display.js")
    confirmation = client.get("/confirmation.js")
    product_shell = client.get("/product-shell.js")

    assert manifest.status_code == 200
    assert theme.status_code == 200
    assert location_display.status_code == 200
    assert "text/javascript" in location_display.headers["content-type"]
    assert "window.MagpieLocationDisplay" in location_display.text
    assert confirmation.status_code == 200
    assert "text/javascript" in confirmation.headers["content-type"]
    assert "window.MagpieConfirmation" in confirmation.text
    assert product_shell.status_code == 200
    assert "text/javascript" in product_shell.headers["content-type"]
    assert "window.MagpieProductShell" in product_shell.text
    assert theme.headers["content-type"].startswith("text/css")
    assert theme.headers["cache-control"] == "no-cache"
    assert "--accent: #0f766e;" in theme.text
    assert manifest.json()["short_name"] == "Magpie"
    assert manifest.json()["display"] == "standalone"
    assert manifest.json()["theme_color"] == "#0f766e"
    assert manifest.json()["id"] == "/"
    assert manifest.json()["start_url"] == "/mobile?source=pwa"
    assert manifest.json()["icons"][0]["src"] == "/app-icon-192.png"
    assert manifest.json()["icons"][1]["src"] == "/app-icon-512.png"
    assert manifest.json()["icons"][2]["purpose"] == "maskable"
    assert [shortcut["short_name"] for shortcut in manifest.json()["shortcuts"]] == [
        "Find",
        "Put away",
        "Items",
        "Add place",
    ]
    assert [shortcut["url"] for shortcut in manifest.json()["shortcuts"]] == [
        "/mobile?action=find&source=shortcut",
        "/mobile?action=put-away&source=shortcut",
        "/storage?view=inventory&source=shortcut",
        "/storage?view=places&create=top&source=shortcut",
    ]
    assert mobile.status_code == 200
    assert "Magpie" in mobile.text
    assert mobile.headers["cache-control"] == "no-cache, no-store, must-revalidate"
    build_version = client.get("/build").json()["commit"][:16]
    for asset, attribute in [
        ("theme.css", "href"),
        ("product-shell.js", "src"),
        ("auth.js", "src"),
        ("location-display.js", "src"),
        ("confirmation.js", "src"),
        ("photo-utils.js", "src"),
    ]:
        assert f'{attribute}="/{asset}?v={build_version}"' in mobile.text
    assert icon.status_code == 200
    assert "image/svg+xml" in icon.headers["content-type"]
    assert "<svg" in icon.text
    assert qr_decoder.status_code == 200
    assert "text/javascript" in qr_decoder.headers["content-type"]
    assert "jsQR 1.4.0" in qr_decoder.text[:120]
    assert qr_decoder.headers["cache-control"] == "public, max-age=604800"
    assert photo_utils.status_code == 200
    assert "text/javascript" in photo_utils.headers["content-type"]
    assert "window.magpiePhotos" in photo_utils.text
    assert photo_utils.headers["cache-control"] == "public, max-age=604800"
    for response, size in [(icon_192, 192), (icon_512, 512), (apple_icon, 180)]:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/png")
        assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert int.from_bytes(response.content[16:20], "big") == size
        assert int.from_bytes(response.content[20:24], "big") == size
        assert response.headers["cache-control"] == "public, max-age=604800"


def test_service_worker_serves_installable_app_shell_cache(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    response = client.get("/service-worker.js")

    assert response.status_code == 200
    assert "text/javascript" in response.headers["content-type"]
    assert 'CACHE_NAME = "magpie-shell-' in response.text
    assert 'SCAN_CACHE_NAME = "magpie-scan-shells-v2"' in response.text
    assert '"/app-icon-192.png"' in response.text
    assert '"/theme.css?v=' in response.text
    assert (
        'NETWORK_FIRST_SHELL = new Set(["/theme.css", "/product-shell.js", '
        '"/auth.js", "/location-display.js", "/confirmation.js", "/photo-utils.js"])'
        in response.text
    )
    assert "NETWORK_FIRST_SHELL.has(url.pathname)" in response.text
    assert 'fetch(event.request, {cache: "no-store"})' in response.text
    assert "caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy))" in response.text
    assert '"/mobile"' in response.text
    assert '"/review"' in response.text
    assert '"/jsqr.min.js"' in response.text
    assert '"/photo-utils.js?v=' in response.text
    assert '"/location-display.js?v=' in response.text
    assert '"/confirmation.js?v=' in response.text
    assert '"/product-shell.js?v=' in response.text
    assert '"/app-icon-512.png"' in response.text
    assert '"/apple-touch-icon.png"' in response.text
    assert 'event.data.type === "CACHE_SCAN_PAGE"' in response.text
    assert 'path.startsWith("/scan/")' in response.text
    assert 'url.pathname.startsWith("/scan/")' in response.text
    assert "const cachedPath = url.pathname;" in response.text
    assert "cache.put(cachedPath, copy)" in response.text
    assert "caches.match(cachedPath).then((cached) => cached || caches.match(\"/\"))" in response.text
    assert 'url.pathname.startsWith("/l/")' not in response.text
    assert 'fetch(event.request, {cache: "no-store"})' in response.text
    assert 'caches.match("/")' in response.text
    assert response.headers["cache-control"] == "no-cache, no-store, must-revalidate"


def test_app_shell_disables_http_cache(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )

    response = TestClient(create_app(cfg)).get("/")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache, no-store, must-revalidate"


def test_responsive_ask_is_canonical_and_cabinet_console_remains_available(
    tmp_path,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
            )
        )
    )

    canonical = client.get("/")
    compatibility = client.get("/mobile")
    cabinet = client.get("/cabinet")
    review = client.get("/review")

    assert canonical.status_code == 200
    assert 'id="cabinetTools"' in canonical.text
    assert 'window.magpieMobileRedirect' in canonical.text
    assert 'window.location.replace("/mobile" + window.location.search + window.location.hash)' in canonical.text
    assert 'id="feed"' in compatibility.text
    assert 'id="cabinetTools"' not in compatibility.text
    assert compatibility.status_code == 200
    assert cabinet.status_code == 200
    assert 'id="cabinetTools"' in cabinet.text
    assert (
        '<span class="product-context" data-product-context>Assistant</span>'
        in cabinet.text
    )
    assert '<script src="/product-shell.js?v=' in cabinet.text
    assert "data-product-nav data-include-cabinet" not in cabinet.text
    assert cabinet.headers["cache-control"] == "no-cache, no-store, must-revalidate"
    assert review.status_code == 200
    assert '<h2>Review</h2>' in review.text
    assert 'id="itemReviewList"' in review.text
    assert review.headers["cache-control"] == "no-cache, no-store, must-revalidate"


def test_clear_bin_inventory_endpoint(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    first = client.post(
        "/inventory",
        json={"name": "First item", "bin_id": "B001", "quantity": 1, "unit": "each"},
    )
    second = client.post(
        "/inventory",
        json={"name": "Second item", "bin_id": "B002", "quantity": 1, "unit": "each"},
    )
    assert first.status_code == 200
    assert second.status_code == 200

    response = client.delete("/bins/B001/inventory")

    assert response.status_code == 200
    assert response.json()["removed"] == 1
    assert response.json()["interaction_id"] > 0
    assert client.get("/bins/B001").json()["inventory"] == []
    assert client.get("/bins/B002").json()["inventory"][0]["name"] == "Second item"
    interaction = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert interaction["id"] == response.json()["interaction_id"]
    assert interaction["route"] == "/inventory/manual"
    assert interaction["status"] == "manual"
    assert interaction["response"]["action"] == "manual_clear_inventory"


def test_inventory_archive_and_restore_endpoints_preserve_placement(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "searchable_storage_led.controller.WledWriter.write_pixels",
        lambda self, pixels, config: None,
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    created = client.post(
        "/inventory",
        json={"name": "Retired jig", "bin_id": "B001", "quantity": 1, "unit": "each"},
    ).json()
    item_id = created["item_id"]
    found = client.post(
        "/assistant/find",
        json={"query": "Retired jig"},
    ).json()

    archived = client.post(f"/inventory/{item_id}/archive")

    assert archived.status_code == 200
    assert found["active_finds"]
    assert archived.json()["active_finds"] == []
    assert archived.json()["active_find_light_ok"] is True
    assert archived.json()["active_find_light_errors"] == []
    assert client.get("/inventory").json()["items"] == []
    [archived_item] = client.get("/inventory?archived=true").json()["items"]
    assert archived_item["active"] is False
    assert archived_item["bin_id"] == "B001"
    assert client.get("/search?q=Retired%20jig").json()["results"] == []

    restored = client.post(f"/inventory/{item_id}/restore")

    assert restored.status_code == 200
    assert restored.json()["active_finds"] == []
    [active_item] = client.get("/inventory").json()["items"]
    assert active_item["active"] is True
    assert active_item["bin_id"] == "B001"


def test_inventory_merge_endpoints_preserve_old_item_routes(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "searchable_storage_led.controller.WledWriter.write_pixels",
        lambda self, pixels, config: None,
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    source = client.post(
        "/inventory",
        json={"name": "M4 tee nuts", "bin_id": "B001", "quantity": 2, "unit": "each"},
    ).json()
    target = client.post(
        "/inventory",
        json={"name": "M4 T-nuts", "bin_id": "B001", "quantity": 3, "unit": "each"},
    ).json()
    found = client.post(
        "/assistant/find",
        json={"query": "M4 tee nuts"},
    ).json()

    preview = client.get(
        f"/inventory/{source['item_id']}/merge-preview",
        params={"target_item_id": target["item_id"]},
    )
    merged = client.post(
        f"/inventory/{source['item_id']}/merge",
        json={"target_item_id": target["item_id"]},
    )

    assert preview.status_code == 200
    assert preview.json()["placements"][0]["quantity"] == 5
    assert merged.status_code == 200
    active = merged.json()["active_finds"]
    assert len(active) == 1
    assert active[0]["id"] == found["active_finds"][0]["id"]
    assert active[0]["color"] == found["active_finds"][0]["color"]
    assert active[0]["matches"][0]["item_id"] == target["item_id"]
    assert active[0]["matches"][0]["name"] == "M4 T-nuts"
    assert active[0]["matches"][0]["location_id"] == "B001"
    assert merged.json()["active_find_light_ok"] is True
    assert merged.json()["active_find_light_errors"] == []
    [item] = client.get("/inventory").json()["items"]
    assert item["item_id"] == target["item_id"]
    assert item["quantity"] == 5
    old_scan = client.get(f"/i/{source['item_id']}")
    assert old_scan.status_code == 200
    assert "M4 T-nuts" in old_scan.text
    old_label = client.get(f"/items/{source['item_id']}/label.pdf")
    assert old_label.status_code == 200
    history = client.get(f"/items/{source['item_id']}/history").json()
    assert history["item_id"] == target["item_id"]
    assert history["requested_item_id"] == source["item_id"]


def test_forced_openai_backend_requires_api_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
        assistant_backend="openai",
    )
    client = TestClient(create_app(cfg))

    status_response = client.get("/assistant/status")
    assert status_response.json()["effective_backend"] == "openai_unconfigured"
    assert status_response.json()["ready"] is False

    command_response = client.post(
        "/assistant/command",
        json={"text": "put this cable away", "photo_ids": []},
    )

    assert command_response.status_code == 502
    # Provider-agnostic: the forced backend reports its configured key is unset
    # (default provider is OpenRouter).
    assert "not set" in command_response.text
    assert "OPENROUTER_API_KEY" in command_response.text


def test_assistant_find_lights_all_matching_bins(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=10,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    client.post("/inventory", json={"name": "M3 screw", "bin_id": "B001"})
    client.post("/inventory", json={"name": "M3 nut", "bin_id": "B002"})

    resp = client.post(
        "/assistant/command",
        json={"text": "find m3", "photo_ids": []},
    )
    body = resp.json()
    assert body["decision"]["action"] == "find_item"
    automation = body["automation"]
    assert automation["lit"] is True
    assert set(automation["bin_ids"]) == {"B001", "B002"}
    assert automation["match_count"] == 2
    assert automation["truncated"] is False


def test_update_inventory_edits_fields_in_place(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "searchable_storage_led.controller.WledWriter.write_pixels",
        lambda self, pixels, config: None,
    )
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    created = client.post(
        "/inventory",
        json={"name": "Cable", "bin_id": "B001", "quantity": 1, "unit": "each"},
    )
    item_id = created.json()["item_id"]
    found = client.post(
        "/assistant/find",
        json={"query": "Cable"},
    ).json()

    updated = client.post(
        f"/inventory/{item_id}",
        json={
            "name": "USB-C Cable",
            "bin_id": "B001",
            "quantity": 5,
            "unit": "pack",
            "category": "electronics",
            "aliases": ["charging cable"],
            "description": "braided",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["interaction_id"] > 0
    active = updated.json()["active_finds"]
    assert active[0]["id"] == found["active_finds"][0]["id"]
    assert active[0]["color"] == found["active_finds"][0]["color"]
    assert active[0]["matches"][0]["name"] == "USB-C Cable"
    assert active[0]["matches"][0]["item_id"] == item_id

    bin_items = client.get("/bins/B001").json()["inventory"]
    assert len(bin_items) == 1
    item = bin_items[0]
    assert item["item_id"] == item_id
    assert item["name"] == "USB-C Cable"
    assert item["quantity"] == 5
    assert item["unit"] == "pack"
    assert item["category"] == "electronics"
    assert item["aliases"] == ["charging cable"]
    interaction = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert interaction["id"] == updated.json()["interaction_id"]
    assert interaction["route"] == "/inventory/manual"
    assert interaction["status"] == "manual"
    assert interaction["response"]["action"] == "manual_update_inventory"
    assert interaction["response"]["item"] == "USB-C Cable"

    undone = client.post("/assistant/undo").json()
    assert undone["ok"] is True
    [restored] = client.get("/bins/B001").json()["inventory"]
    assert restored["name"] == "Cable"
    assert restored["quantity"] == 1
    assert restored["unit"] == "each"
    assert restored["category"] == ""
    assert restored["aliases"] == []
    active_after_undo = client.get("/lights/active").json()["active_finds"]
    assert active_after_undo[0]["matches"][0]["name"] == "Cable"


def test_inventory_create_and_update_persist_item_size(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))

    created = client.post(
        "/inventory",
        json={
            "name": "Filament spool",
            "bin_id": "B001",
            "size_class": "large",
            "dimensions": {"length": 200, "width": 200, "height": 70, "unit": "mm"},
        },
    )
    assert created.status_code == 200
    item_id = created.json()["item_id"]
    item = client.get("/bins/B001").json()["inventory"][0]
    assert item["size_class"] == "large"
    assert item["dimensions"] == {"length": 200, "width": 200, "height": 70, "unit": "mm"}

    updated = client.post(
        f"/inventory/{item_id}",
        json={
            "name": "Filament spool",
            "bin_id": "B001",
            "quantity": 1,
            "unit": "each",
            "size_class": "medium",
            "dimensions": {"note": "half-used spool"},
        },
    )
    assert updated.status_code == 200
    item = client.get("/bins/B001").json()["inventory"][0]
    assert item["size_class"] == "medium"
    assert item["dimensions"] == {"note": "half-used spool"}
    assert InventoryStore(db_path).item_size_class("Filament spool") == "medium"

    # Older clients omitting size must not erase metadata.
    assert client.post(
        f"/inventory/{item_id}",
        json={"name": "Filament spool", "bin_id": "B001", "quantity": 2},
    ).status_code == 200
    item = client.get("/bins/B001").json()["inventory"][0]
    assert item["size_class"] == "medium"
    assert item["dimensions"] == {"note": "half-used spool"}

    assert client.post(
        f"/inventory/{item_id}",
        json={"name": "Filament spool", "bin_id": "B001", "size_class": ""},
    ).status_code == 200
    item = client.get("/bins/B001").json()["inventory"][0]
    assert item["size_class"] == ""
    assert not item["dimensions"]
    assert item["size_source"] == ""
    assert item["size_confidence"] == 0
    assert InventoryStore(db_path).item_size_class("Filament spool") is None

    undone = client.post("/assistant/undo")
    assert undone.status_code == 200
    assert undone.json()["ok"] is True
    item = client.get("/bins/B001").json()["inventory"][0]
    assert item["size_class"] == "medium"
    assert item["dimensions"] == {"note": "half-used spool"}
    assert item["size_source"] == "manual"
    assert item["size_confidence"] == 1

    previous_metadata = {key: item[key] for key in ("size_class", "size_source", "size_confidence", "dimensions")}
    assert client.post(
        f"/inventory/{item_id}",
        json={
            "name": "Filament spool", "bin_id": "B001", "size_class": "medium",
            "dimensions": {"note": "new measurement"}, "size_source": "measured",
            "size_confidence": 0.75,
        },
    ).status_code == 200
    assert client.post("/assistant/undo").json()["ok"] is True
    item = client.get("/bins/B001").json()["inventory"][0]
    assert {key: item[key] for key in previous_metadata} == previous_metadata


def test_inventory_home_update_is_visible_and_undoable(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 3,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 3, "spans": [1, 1, 1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=3,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    created = client.post("/inventory", json={"name": "M4 T-nuts", "bin_id": "B001"})
    item_id = created.json()["item_id"]
    assert client.post(
        "/inventory/move",
        json={"item_id": item_id, "from_bin": "B001", "to_bin": "B002"},
    ).status_code == 200

    updated = client.post(
        f"/inventory/{item_id}/home",
        json={"location_id": "B003"},
    )

    assert updated.status_code == 200
    body = updated.json()
    assert body["home_location_id"] == "B003"
    assert body["interaction_id"] > 0
    item = client.get("/bins/B002").json()["inventory"][0]
    assert item["home_location_id"] == "B003"
    assert item["last_seen_location_id"] == "B002"
    interaction = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert interaction["route"] == "/inventory/home"
    assert interaction["status"] == "confirmed"
    assert interaction["response"]["applied"][0]["previous_home_location_id"] == "B001"

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    assert undone["undone"][0]["restored_item_home"] == item_id
    restored = client.get("/bins/B002").json()["inventory"][0]
    assert restored["home_location_id"] == "B001"
    assert restored["last_seen_location_id"] == "B002"


def test_placement_size_explains_item_capacity_source(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(db_path)
    item_id = store.upsert_item(name="Filament spool")
    store.update_item_size(item_id=item_id, size_class="large", source="manual")
    store.learn_size_rule(
        rule_text="Jewelry boxes are medium.",
        trigger_terms=["jewelry", "box"],
        size_class="medium",
    )

    learned_item = client.post("/placement/size", json={"item": "Filament spool"}).json()
    learned_rule = client.post("/placement/size", json={"item": "jewelry box"}).json()
    inferred = client.post("/placement/size", json={"item": "M3 screw"}).json()

    assert learned_item["size_class"] == "large"
    assert learned_item["reason"] == "learned item size"
    assert learned_rule["size_class"] == "medium"
    assert learned_rule["reason"] == "learned size rule: Jewelry boxes are medium."
    assert inferred["size_class"] == "tiny"
    assert inferred["reason"] == "inferred from item words"


def test_calibrated_photo_measurement_uses_uploaded_repository_content(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    llm = MeasurementLlm()
    client = TestClient(create_app(cfg, llm=llm))
    uploaded = client.post(
        "/photos",
        json={
            "filename": "tool.jpg",
            "content_type": "image/jpeg",
            "data_base64": "data:image/jpeg;base64," + base64.b64encode(b"photo").decode("ascii"),
        },
    ).json()["photo"]

    response = client.post(
        "/measurement/photo",
        json={"item": "blue pry tool", "photo_ids": [uploaded["id"]]},
    )
    grid = client.get("/measurement/calibration-grid.pdf")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["dimensions"] == {"length_mm": 74.8, "width_mm": 18.2}
    assert response.json()["size_class"] == "medium"
    assert response.json()["size_source"] == "inference"
    assert llm.call["images"][0] == (
        "data:image/jpeg;base64," + base64.b64encode(b"photo").decode("ascii")
    )
    assert grid.status_code == 200
    assert grid.headers["content-type"] == "application/pdf"
    assert grid.content.startswith(b"%PDF-1.4")


def test_manual_inventory_records_measured_size_provenance(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    db_path = tmp_path / "inventory.db"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(db_path),
            )
        )
    )

    response = client.post(
        "/inventory",
        json={
            "name": "Blue pry tool",
            "bin_id": "B001",
            "size_class": "medium",
            "size_source": "measurement",
            "size_confidence": 0.84,
            "dimensions": {"length_mm": 74.8, "width_mm": 18.2},
        },
    )

    assert response.status_code == 200
    item = InventoryStore(db_path).items_in_location("B001")[0]
    assert item.size_class == "medium"
    assert item.size_source == "measurement"
    assert item.size_confidence == 0.84
    assert item.dimensions == {"length_mm": 74.8, "width_mm": 18.2}


def test_update_review_item_records_review_correction_example(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    db_path = tmp_path / "inventory.db"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    item_id = InventoryStore(db_path).upsert_item(name="generic nuts")
    InventoryStore(db_path).put_inventory(
        item_id=item_id,
        bin_id="B001",
        quantity=1,
        unit="each",
        needs_review=True,
    )

    updated = client.post(
        f"/inventory/{item_id}",
        json={
            "name": "M4 T-nuts",
            "bin_id": "B001",
            "quantity": 2,
            "unit": "each",
            "category": "hardware",
            "aliases": ["tee nuts"],
            "description": "four-prong threaded inserts",
        },
    )

    assert updated.status_code == 200
    assert client.get("/inventory/review").json()["items"] == []
    example = InventoryStore(db_path).recent_examples(limit=1)[0]
    assert example["kind"] == "review_correction"
    assert example["command"] == "review corrected generic nuts to M4 T-nuts"
    payload = json.loads(example["action"])
    assert payload["before"]["name"] == "generic nuts"
    assert payload["before"]["needs_review"] is True
    assert payload["after"]["name"] == "M4 T-nuts"
    assert payload["after"]["aliases"] == ["tee nuts"]


def test_update_inventory_rejects_duplicate_name(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    client.post("/inventory", json={"name": "Cable", "bin_id": "B001"})
    second = client.post("/inventory", json={"name": "Washer", "bin_id": "B001"})
    second_id = second.json()["item_id"]

    clash = client.post(
        f"/inventory/{second_id}",
        json={"name": "Cable", "bin_id": "B001"},
    )
    assert clash.status_code == 400


def test_assistant_find_lights_the_matching_bin(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    store_response = client.post(
        "/assistant/command",
        json={"text": "put this cable away", "photo_ids": []},
    )
    assert store_response.json()["automation"]["auto_saved"] is True

    find_response = client.post(
        "/assistant/command",
        json={"text": "where is the cable", "photo_ids": []},
    )
    automation = find_response.json()["automation"]
    assert find_response.json()["decision"]["action"] == "find_item"
    assert automation["lit"] is True
    assert automation["bin_id"] == "B001"
    assert automation["light_seconds"] == 180.0


def test_assistant_eval_export_and_replay_use_confirmed_examples(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(tmp_path / "inventory.db")
    store.record_example(
        command="historical put away",
        action=json.dumps(
            [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "historical item",
                        "location_id": "B045",
                    },
                }
            ]
        ),
        kind="confirmed",
        context={"photo_ids": [42], "source_interaction_id": 7},
    )

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "put m4 t-nuts in B001",
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "M4 T-nuts",
                        "location_id": "B001",
                        "quantity": 2,
                    },
                }
            ],
        },
    )
    assert confirmed.status_code == 200

    exported = client.get("/assistant/evals/export?limit=10&kind=confirmed").json()

    assert exported["ok"] is True
    assert exported["count"] == 2
    historical = exported["examples"][1]
    assert historical["expected_plan"][0]["arguments"]["location_id"] == "B045"
    assert historical["context"] == {"photo_ids": [42], "source_interaction_id": 7}
    example = exported["examples"][0]
    assert example["command"] == "put m4 t-nuts in B001"
    assert example["expected_tools"] == ["put_item"]
    assert example["expected_plan"][0]["arguments"]["item"] == "M4 T-nuts"
    assert example["created_at"]

    pending_replay = client.post(
        "/assistant/evals/replay",
        json={"limit": 10, "kind": "confirmed", "run_model": False},
    ).json()
    assert pending_replay["count"] == 0
    assert pending_replay["excluded_unreviewed"] == 2
    assert pending_replay["gate_passed"] is False

    for row in store.examples_for_eval(limit=10, kind="confirmed"):
        assert store.approve_example(int(row["id"])) is True

    replay = client.post(
        "/assistant/evals/replay",
        json={
            "limit": 10,
            "minimum_cases": 1,
            "required_scenarios": ["putaway"],
            "kind": "confirmed",
            "run_model": False,
        },
    ).json()

    assert replay["ok"] is True
    assert replay["run_model"] is False
    assert replay["minimum_cases"] == 1
    assert replay["eligible_review_statuses"] == ["approved", "promoted"]
    assert replay["eligible_count"] == 2
    assert replay["excluded_unreviewed"] == 0
    assert replay["evidence_count"] == 1
    assert replay["evaluated"] == 0
    assert replay["gate_passed"] is True
    assert replay["ready"] == 1
    assert replay["skipped"] == 1
    assert replay["failed"] == 0
    assert replay["scenario_coverage"] == {"putaway": 1}
    assert replay["required_scenarios"] == ["putaway"]
    assert replay["missing_required_scenarios"] == []
    assert replay["results"][0]["expected_tools"] == ["put_item"]

    missing_photo_coverage = client.post(
        "/assistant/evals/replay",
        json={
            "limit": 10,
            "minimum_cases": 1,
            "required_scenarios": ["photo"],
            "kind": "confirmed",
            "run_model": False,
        },
    ).json()
    assert missing_photo_coverage["evidence_count"] == 1
    assert missing_photo_coverage["missing_required_scenarios"] == ["photo"]
    assert missing_photo_coverage["gate_passed"] is False


def test_assistant_eval_replay_uses_semantic_plan_matching_and_skips_contextless_photos(
    tmp_path,
    monkeypatch,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    store = InventoryStore(tmp_path / "inventory.db")
    store.record_example(
        command="put USB adapter in B001",
        action=json.dumps(
            [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "USB adapter",
                        "location_id": "B001",
                        "quantity": 1,
                    },
                }
            ]
        ),
        kind="confirmed",
    )
    store.record_example(
        command="photo",
        action=json.dumps(
            [
                {
                    "tool": "put_item",
                    "arguments": {"item": "M3 screws", "location_id": "B001"},
                }
            ]
        ),
        kind="confirmed",
    )
    store.record_example(
        command="Phillis head\nThese are wood screws and need their own bin.",
        action=json.dumps(
            [
                {
                    "tool": "put_item",
                    "arguments": {"item": "Wood screws", "location_id": "B001"},
                }
            ]
        ),
        kind="confirmed",
    )
    store.record_example(
        command="put Velcro strip in B001",
        action=json.dumps(
            [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "Velcro strip",
                        "location_id": "B001",
                        "quantity": 1,
                    },
                }
            ]
        ),
        kind="confirmed",
    )
    for command in (
        "They should fit in a small bin.",
        "I have this bin of supplies; make out what you can.",
        "smoke create attic and roaster bin",
    ):
        store.record_example(
            command=command,
            action=json.dumps(
                [
                    {
                        "tool": "put_item",
                        "arguments": {"item": "context item", "location_id": "B001"},
                    }
                ]
            ),
            kind="confirmed",
        )
    LocationStore(tmp_path / "inventory.db").upsert(
        Location(
            id="archive-bin",
            type="bin",
            label="Archive Bin",
            capacity_class="small",
        )
    )
    archive_id = store.upsert_item(name="archive notes")
    store.put_inventory(item_id=archive_id, bin_id="archive-bin", quantity=1, unit="each")
    store.record_example(
        command="Create Archive Bin and put archive notes in it",
        action=json.dumps(
            [
                {
                    "tool": "create_location",
                    "arguments": {
                        "type": "bin",
                        "label": "Archive Bin",
                        "capacity_class": "small",
                    },
                },
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "archive notes",
                        "location_id": "archive-bin",
                        "quantity": 1,
                    },
                },
            ]
        ),
        kind="confirmed",
    )
    for row in store.examples_for_eval(limit=20, kind="confirmed"):
        assert store.approve_example(int(row["id"])) is True

    def replay_agent(text, **kwargs):
        if "Velcro" in text or "Archive Bin" in text:
            return AgentResult(
                message="Already stored",
                plan=[],
                trace=[] if "Archive Bin" in text else [
                    {
                        "tool": "search_items",
                        "arguments": {"query": "Velcro strip"},
                        "result": [
                            {
                                "name": "Velcro strips",
                                "location_id": "B001",
                                "quantity": 1.0,
                            }
                        ],
                    }
                ],
            )
        return AgentResult(
            message="Ready",
            plan=[
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "USB adapters",
                        "location_id": "B001",
                        "quantity": 1,
                        "unit": "each",
                    },
                }
            ],
            trace=[],
        )

    monkeypatch.setattr(
        "searchable_storage_led.api.run_agent",
        replay_agent,
    )
    client = TestClient(create_app(cfg))

    replay = client.post(
        "/assistant/evals/replay",
        json={
            "limit": 10,
            "minimum_cases": 4,
            "required_scenarios": ["putaway", "organization"],
            "kind": "confirmed",
            "run_model": True,
        },
    ).json()

    assert replay["passed"] == 3
    assert replay["failed"] == 0
    assert replay["skipped"] == 5
    assert replay["evaluated"] == 3
    assert replay["evidence_count"] == 3
    assert replay["minimum_cases"] == 4
    assert replay["scenario_coverage"] == {
        "organization": 1,
        "putaway": 3,
    }
    assert replay["missing_required_scenarios"] == []
    assert replay["gate_passed"] is False
    statuses = {row["command"]: row for row in replay["results"]}
    assert statuses["put USB adapter in B001"]["status"] == "passed"
    assert statuses["put USB adapter in B001"]["differences"] == []
    assert statuses["put Velcro strip in B001"]["status"] == "passed"
    assert statuses["put Velcro strip in B001"]["satisfied_by_state"] is True
    archive = statuses["Create Archive Bin and put archive notes in it"]
    assert archive["status"] == "passed"
    assert archive["satisfied_by_state"] is True
    assert statuses["photo"]["status"] == "skipped"
    assert "no replayable photo context" in statuses["photo"]["reason"]
    ambiguous = statuses["Phillis head\nThese are wood screws and need their own bin."]
    assert ambiguous["status"] == "skipped"
    assert "no replayable interaction context" in ambiguous["reason"]
    assert statuses["They should fit in a small bin."]["status"] == "skipped"
    assert statuses["I have this bin of supplies; make out what you can."]["status"] == "skipped"
    assert statuses["smoke create attic and roaster bin"]["status"] == "skipped"


def test_assistant_eval_replay_rejects_impossible_evidence_floor(
    tmp_path,
) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        backend="none",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))

    response = client.post(
        "/assistant/evals/replay",
        json={
            "limit": 25,
            "minimum_cases": 26,
            "kind": "confirmed",
            "run_model": False,
        },
    )

    assert response.status_code == 422


def test_assistant_learning_examples_expose_recent_corrections(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / "inventory.db"),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(tmp_path / "inventory.db")
    store.record_example(
        command="item corrected M4 nut to M4 T-nut",
        action=json.dumps(
            {
                "before": {"name": "M4 nut", "category": "hardware"},
                "after": {"name": "M4 T-nut", "category": "hardware"},
            }
        ),
        kind="item_correction",
    )
    store.record_example(command="put blue pry tool away", action="[]", kind="confirmed")

    response = client.get("/assistant/learning/examples?limit=10&kind=item_correction")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["count"] == 1
    example = body["examples"][0]
    assert example["kind"] == "item_correction"
    assert example["review_status"] == "pending"
    assert example["command"] == "item corrected M4 nut to M4 T-nut"
    assert example["parsed_action"]["after"]["name"] == "M4 T-nut"
    assert example["scenarios"] == ["correction"]
    assert example["created_at"]


def test_assistant_learning_examples_expose_review_approvals(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(db_path)
    store.record_example(
        command="review approved M4 T-nuts in B001",
        action=json.dumps(
            {
                "approved_review": {
                    "name": "M4 T-nuts",
                    "bin_id": "B001",
                    "quantity": 2,
                    "review_reason": "Agent marked this item for review.",
                }
            }
        ),
        kind="review_approved",
    )

    response = client.get("/assistant/learning/examples?limit=10&kind=review_approved")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["count"] == 1
    example = body["examples"][0]
    assert example["kind"] == "review_approved"
    assert example["command"] == "review approved M4 T-nuts in B001"
    assert example["parsed_action"]["approved_review"]["name"] == "M4 T-nuts"
    assert example["parsed_action"]["approved_review"]["review_reason"] == "Agent marked this item for review."


def test_reject_learning_example_removes_it_from_future_examples(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(db_path)
    store.record_example(
        command="bad example",
        action=json.dumps([{"tool": "put_item", "arguments": {"item": "wrong", "location_id": "B001"}}]),
        kind="confirmed",
    )
    example_id = store.examples_for_eval(limit=1)[0]["id"]

    rejected = client.delete(f"/assistant/learning/examples/{example_id}")

    assert rejected.status_code == 200
    assert rejected.json() == {"ok": True, "rejected": example_id, "rolled_back": []}
    assert store.examples_for_eval(limit=10) == []
    assert store.find_examples("bad example", limit=3) == []
    rejected_example = store.example(example_id)
    assert rejected_example is not None
    assert rejected_example["review_status"] == "rejected"
    assert rejected_example["reviewed_at"]
    all_examples = store.examples_for_eval(limit=10, include_rejected=True)
    assert len(all_examples) == 1
    assert all_examples[0]["review_status"] == "rejected"
    approve_rejected = client.post(f"/assistant/learning/examples/{example_id}/approve")
    assert approve_rejected.status_code == 409

    missing = client.delete(f"/assistant/learning/examples/{example_id}")
    assert missing.status_code == 200


def test_approve_learning_example_marks_reviewed_but_keeps_it_usable(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(db_path)
    store.record_example(
        command="put good m3 bolts away",
        action=json.dumps([{"tool": "put_item", "arguments": {"item": "M3 bolts", "location_id": "B001"}}]),
        kind="confirmed",
    )
    example_id = store.examples_for_eval(limit=1)[0]["id"]

    approved = client.post(f"/assistant/learning/examples/{example_id}/approve")

    assert approved.status_code == 200
    assert approved.json() == {"ok": True, "approved": example_id}
    example = store.example(example_id)
    assert example is not None
    assert example["review_status"] == "approved"
    assert example["reviewed_at"]
    assert store.find_examples("where are good m3 bolts", limit=3)[0]["review_status"] == "approved"
    listed = client.get("/assistant/learning/examples?limit=1&review_status=approved").json()
    assert listed["count"] == 1
    assert listed["examples"][0]["review_status"] == "approved"


def test_bulk_approve_learning_examples_marks_only_pending_examples(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(db_path)
    store.record_example(command="first pending", action="[]", kind="confirmed")
    first_id = store.examples_for_eval(limit=1)[0]["id"]
    store.record_example(command="second pending", action="[]", kind="confirmed")
    second_id = store.examples_for_eval(limit=1)[0]["id"]
    store.record_example(command="already rejected", action="[]", kind="confirmed")
    rejected_id = store.examples_for_eval(limit=1)[0]["id"]
    store.reject_example(rejected_id)

    response = client.post(
        "/assistant/learning/examples/bulk-approve",
        json={"example_ids": [first_id, second_id, second_id, rejected_id, 9999]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["approved"] == [first_id, second_id]
    assert body["approved_count"] == 2
    assert body["skipped"] == [
        {"id": rejected_id, "reason": "rejected"},
        {"id": 9999, "reason": "not_found"},
    ]
    assert store.example(first_id)["review_status"] == "approved"
    assert store.example(second_id)["review_status"] == "approved"
    assert store.example(rejected_id)["review_status"] == "rejected"


def test_learning_examples_filter_by_review_status(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(db_path)
    store.record_example(command="pending example", action="[]", kind="confirmed")
    pending_id = store.examples_for_eval(limit=1)[0]["id"]
    store.record_example(command="approved example", action="[]", kind="confirmed")
    approved_id = store.examples_for_eval(limit=1)[0]["id"]
    store.approve_example(approved_id)

    pending = client.get("/assistant/learning/examples?limit=10&review_status=pending").json()
    approved = client.get("/assistant/learning/examples?limit=10&review_status=approved").json()

    assert [example["id"] for example in pending["examples"]] == [pending_id]
    assert pending["examples"][0]["command"] == "pending example"
    assert [example["id"] for example in approved["examples"]] == [approved_id]
    assert approved["examples"][0]["command"] == "approved example"

    scenario_examples = [
        (
            "put photo item away",
            [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "Photo item",
                        "location_id": "B001",
                    },
                }
            ],
            "confirmed",
            {"photo_ids": [17]},
        ),
        (
            "move cabinet into garage",
            [
                {
                    "tool": "move_container",
                    "arguments": {
                        "location_id": "cabinet",
                        "new_parent_id": "garage",
                    },
                }
            ],
            "manual",
            {},
        ),
        (
            "correct the item destination",
            [
                {
                    "tool": "move_item",
                    "arguments": {
                        "item_id": 1,
                        "to_location_id": "B001",
                    },
                }
            ],
            "correction",
            {},
        ),
        (
            "hardware belongs in the cabinet",
            [
                {
                    "tool": "learn_rule",
                    "arguments": {
                        "rule_text": "Hardware belongs here",
                    },
                }
            ],
            "confirmed",
            {},
        ),
    ]
    for command, action, kind, context in scenario_examples:
        store.record_example(
            command=command,
            action=json.dumps(action),
            kind=kind,
            context=context,
        )

    expected = {
        "putaway": ("correct the item destination", ["putaway", "correction"]),
        "photo": ("put photo item away", ["putaway", "photo"]),
        "correction": ("correct the item destination", ["putaway", "correction"]),
        "organization": ("move cabinet into garage", ["organization"]),
        "rules": ("hardware belongs in the cabinet", ["rules"]),
    }
    for scenario, (command, scenarios) in expected.items():
        filtered = client.get(
            "/assistant/learning/examples",
            params={"limit": 10, "scenario": scenario},
        )
        assert filtered.status_code == 200
        body = filtered.json()
        assert body["scenario"] == scenario
        matching = next(
            example for example in body["examples"] if example["command"] == command
        )
        assert matching["scenarios"] == scenarios

    invalid = client.get(
        "/assistant/learning/examples",
        params={"scenario": "anything"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "invalid learning scenario"


def test_learning_summary_counts_review_statuses(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(db_path)
    store.record_example(command="pending example", action="[]", kind="confirmed")
    store.record_example(command="approved example", action="[]", kind="manual")
    approved_id = store.examples_for_eval(limit=1)[0]["id"]
    store.approve_example(approved_id)
    store.record_example(command="rejected example", action="[]", kind="correction")
    rejected_id = store.examples_for_eval(limit=1)[0]["id"]
    store.reject_example(rejected_id)

    response = client.get("/assistant/learning/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["review_counts"] == {
        "pending": 1,
        "approved": 1,
        "promoted": 0,
        "rejected": 1,
        "active": 2,
        "total": 3,
    }
    assert body["kind_counts"] == {
        "confirmed": 1,
        "manual": 1,
    }


def test_promote_learning_example_applies_explicit_learn_actions(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(db_path)
    store.record_example(
        command="jewelry always goes in the first drawer and rings are small",
        action=json.dumps(
            [
                {
                    "tool": "learn_rule",
                    "arguments": {
                        "rule_text": "Jewelry goes in the first drawer.",
                        "trigger_terms": ["jewelry"],
                        "target_location_id": "B001",
                    },
                },
                {
                    "tool": "learn_size_rule",
                    "arguments": {
                        "rule_text": "Rings are small.",
                        "trigger_terms": ["rings"],
                        "size_class": "small",
                    },
                },
            ]
        ),
        kind="confirmed",
    )
    example_id = store.examples_for_eval(limit=1)[0]["id"]

    listed = client.get("/assistant/learning/examples?limit=1").json()
    assert listed["examples"][0]["promotable"] is True

    promoted = client.post(f"/assistant/learning/examples/{example_id}/promote")

    assert promoted.status_code == 200
    body = promoted.json()
    assert body["ok"] is True
    assert body["promoted"] == example_id
    assert [entry.get("rule_text") for entry in body["applied"]] == [
        "Jewelry goes in the first drawer.",
        "Rings are small.",
    ]
    placement = store.matching_rules("jewelry")[0]
    assert placement.target_location_id == "B001"
    assert placement.source_command == "jewelry always goes in the first drawer and rings are small"
    size = store.matching_size_rules("rings")[0]
    assert size.size_class == "small"
    assert size.source_command == "jewelry always goes in the first drawer and rings are small"
    promoted_example = store.example(example_id)
    assert promoted_example is not None
    assert promoted_example["review_status"] == "promoted"
    assert promoted_example["reviewed_at"]
    relisted = client.get("/assistant/learning/examples?limit=1").json()
    assert relisted["examples"][0]["review_status"] == "promoted"
    assert relisted["examples"][0]["promotable"] is False
    promoted_again = client.post(f"/assistant/learning/examples/{example_id}/promote")
    assert promoted_again.status_code == 409
    approve_promoted = client.post(f"/assistant/learning/examples/{example_id}/approve")
    assert approve_promoted.status_code == 409


def test_promote_learning_example_rejects_non_learning_actions(tmp_path) -> None:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "inventory.db"
    cfg = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(db_path),
    )
    client = TestClient(create_app(cfg))
    store = InventoryStore(db_path)
    store.record_example(
        command="put away one washer",
        action=json.dumps([{"tool": "put_item", "arguments": {"item": "washer", "location_id": "B001"}}]),
        kind="confirmed",
    )
    example_id = store.examples_for_eval(limit=1)[0]["id"]

    listed = client.get("/assistant/learning/examples?limit=1").json()
    assert listed["examples"][0]["promotable"] is False

    promoted = client.post(f"/assistant/learning/examples/{example_id}/promote")

    assert promoted.status_code == 409
