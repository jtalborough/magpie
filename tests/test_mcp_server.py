import json

from fastapi.testclient import TestClient

from searchable_storage_led.api import create_app
from searchable_storage_led.config import LedConfig
from searchable_storage_led.mcp_server import create_runtime_service


def _app(tmp_path):
    layout = tmp_path / "layout.json"
    layout.write_text(
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
    return create_app(
        LedConfig(
            pixel_count=1,
            backend="wled",
            wled_url="http://127.0.0.1:1",
            layout_path=str(layout),
            database_path=str(tmp_path / "inventory.db"),
        )
    )


def _mcp_call(client: TestClient, name: str, arguments: dict) -> object:
    response = client.post(
        "/mcp/",
        headers={
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-03-26",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    structured = body["result"].get("structuredContent")
    if structured is not None:
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    content = body["result"].get("content") or []
    decoded = [
        json.loads(block["text"])
        for block in content
        if block["type"] == "text"
    ]
    if name in {
        "search_items",
        "list_locations",
        "list_storage_roots",
        "list_archived_locations",
        "suggest_locations",
        "list_review_items",
    }:
        return decoded
    return decoded[0] if len(decoded) == 1 else decoded


def test_mcp_lists_complete_guarded_storage_surface(tmp_path) -> None:
    with TestClient(_app(tmp_path)) as client:
        response = client.post(
            "/mcp/",
            headers={
                "accept": "application/json, text/event-stream",
                "mcp-protocol-version": "2025-03-26",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )

    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert {
        "storage_status",
        "get_storage_contract",
        "search_items",
        "retrieve_item",
        "list_locations",
        "list_storage_roots",
        "list_archived_locations",
        "get_location",
        "suggest_locations",
        "list_rules",
        "list_review_items",
        "plan_reorganization",
        "preview_label_sheet",
        "get_printer_status",
        "propose_storage_plan",
        "light_location",
        "clear_lights",
        "get_pending_plan",
    } <= names
    assert "put_item" not in names


def test_mcp_contract_and_whole_storage_reads_are_self_describing(tmp_path) -> None:
    with TestClient(_app(tmp_path)) as client:
        contract = _mcp_call(client, "get_storage_contract", {})
        roots = _mcp_call(client, "list_storage_roots", {})
        archived = _mcp_call(client, "list_archived_locations", {})

    assert contract["schema"] == "magpie.storage-plan.v1"
    assert contract["confirmation_required"] is True
    write_tools = {tool["name"]: tool for tool in contract["write_tools"]}
    assert "create_location" in write_tools
    assert "archive_location" in write_tools
    assert "restore_location" in write_tools
    assert "move_container" in write_tools
    assert "put_item" in write_tools
    assert roots[0]["id"] == "main-cabinet"
    assert archived == []


def test_mcp_measured_item_dimensions_reject_too_small_locations(tmp_path) -> None:
    with TestClient(_app(tmp_path)) as client:
        client.post(
            "/locations/B001/update",
            json={
                "capacity_class": "small",
                "internal_dimensions": {
                    "width_mm": 100,
                    "depth_mm": 80,
                    "height_mm": 40,
                },
            },
        )
        client.post(
            "/locations",
            json={
                "id": "large-box",
                "type": "box",
                "label": "Large Box",
                "capacity_class": "large",
                "internal_dimensions": {
                    "width_mm": 400,
                    "depth_mm": 300,
                    "height_mm": 200,
                },
            },
        )
        suggestions = _mcp_call(
            client,
            "suggest_locations",
            {
                "item": "camera case",
                "dimensions": {
                    "length_mm": 300,
                    "width_mm": 200,
                    "height_mm": 75,
                },
            },
        )

    ids = {suggestion["location_id"] for suggestion in suggestions}
    assert "large-box" in ids
    assert "B001" not in ids


def test_mcp_proposal_can_be_confirmed_by_voice_without_direct_write(tmp_path) -> None:
    with TestClient(_app(tmp_path)) as client:
        proposal = _mcp_call(
            client,
            "propose_storage_plan",
            {
                "command": "put four spare fuses in B001",
                "steps": [
                    {
                        "tool": "put_item",
                        "arguments": {
                            "item": "spare fuses",
                            "location_id": "B001",
                            "quantity": 4,
                        },
                    }
                ],
            },
        )
        assert proposal["requires_confirmation"] is True
        assert client.get("/inventory").json()["items"] == []

        confirmed = client.post("/assistant/voice/confirm").json()

    assert confirmed["ok"] is True
    assert confirmed["source_interaction_id"] == proposal["interaction_id"]
    assert confirmed["applied"][0]["stored"] == "spare fuses"
    assert confirmed["applied"][0]["quantity"] == 4


def test_mcp_whole_storage_archive_and_restore_stay_confirmation_gated(tmp_path) -> None:
    with TestClient(_app(tmp_path)) as client:
        created = client.post(
            "/locations",
            json={"id": "attic", "type": "area", "label": "Attic"},
        )
        assert created.status_code == 200

        archive = _mcp_call(
            client,
            "propose_storage_plan",
            {
                "command": "Archive the attic",
                "steps": [
                    {
                        "tool": "archive_location",
                        "arguments": {"location_id": "attic"},
                    }
                ],
            },
        )
        assert archive["requires_confirmation"] is True
        assert client.get("/locations/attic").status_code == 200

        archived = client.post(
            "/assistant/confirm",
            json={
                "command": "Archive the attic",
                "plan": archive["plan"],
                "source_interaction_id": archive["interaction_id"],
            },
        ).json()
        assert archived["applied"][0]["archived_location_id"] == "attic"
        assert client.get("/locations/attic").status_code == 404
        archived_locations = _mcp_call(client, "list_archived_locations", {})
        assert archived_locations[0]["id"] == "attic"

        restore = _mcp_call(
            client,
            "propose_storage_plan",
            {
                "command": "Restore the attic",
                "steps": [
                    {
                        "tool": "restore_location",
                        "arguments": {"location_id": "attic"},
                    }
                ],
            },
        )
        assert restore["requires_confirmation"] is True
        assert client.get("/locations/attic").status_code == 404

        restored = client.post(
            "/assistant/confirm",
            json={
                "command": "Restore the attic",
                "plan": restore["plan"],
                "source_interaction_id": restore["interaction_id"],
            },
        ).json()
        assert restored["applied"][0]["restored_location_id"] == "attic"
        assert client.get("/locations/attic").status_code == 200


def test_standalone_mcp_runtime_accepts_target_aware_light_calls(tmp_path) -> None:
    service = create_runtime_service(
        LedConfig(
            pixel_count=0,
            backend="none",
            layout_path="config/storage-layout.json",
            database_path=str(tmp_path / "inventory.db"),
        )
    )

    result = service.light_location("B001", color=(255, 0, 0), seconds=1)

    assert result["ok"] is True
    assert result["lit"] is False
    assert result["lit_location"] == "B001"
