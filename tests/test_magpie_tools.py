import pytest

from searchable_storage_led.layout import StorageLayout
from searchable_storage_led.locations import LocationStore, backfill_from_layout
from searchable_storage_led.magpie_tools import MagpieToolService
from searchable_storage_led.store import InventoryStore


def _service(tmp_path, **kwargs) -> MagpieToolService:
    layout = StorageLayout.from_path("config/storage-layout.json")
    store = InventoryStore(tmp_path / "inventory.db")
    locations = LocationStore(tmp_path / "inventory.db")
    backfill_from_layout(locations, layout)
    return MagpieToolService(
        store=store,
        locations=locations,
        layout=layout,
        **kwargs,
    )


def test_shared_tool_service_reads_complete_storage_paths(tmp_path) -> None:
    service = _service(tmp_path)
    item_id = service.store.upsert_item(name="M4 T-nuts")
    service.store.put_inventory(item_id=item_id, bin_id="B010", quantity=12, unit="each")

    results = service.read("search_items", {"query": "T-nuts"})

    assert results[0]["item_id"] == item_id
    assert results[0]["location_id"] == "B010"
    assert [entry["id"] for entry in results[0]["path"]] == ["main-cabinet", "B010"]
    assert results[0]["nearest_lit_location_id"] == "B010"


def test_mcp_plan_is_pending_and_confirmation_gated(tmp_path) -> None:
    service = _service(tmp_path)

    proposed = service.propose_plan(
        command="put spare fuses in B001",
        steps=[
            {
                "tool": "put_item",
                "arguments": {"item": "spare fuses", "location_id": "B001", "quantity": 4},
            }
        ],
    )

    assert proposed["requires_confirmation"] is True
    assert service.store.list_inventory() == []
    assert service.pending_plan()["interaction_id"] == proposed["interaction_id"]
    assert service.pending_plan()["plan"] == proposed["plan"]


def test_mcp_plan_rejects_unknown_or_unsafe_write_tools(tmp_path) -> None:
    service = _service(tmp_path)

    with pytest.raises(ValueError, match="unsupported write tools"):
        service.propose_plan(
            command="silently rewrite everything",
            steps=[{"tool": "execute_sql", "arguments": {"sql": "delete from items"}}],
        )


def test_storage_contract_is_generated_from_guarded_agent_tools(tmp_path) -> None:
    service = _service(tmp_path)

    contract = service.storage_contract()

    assert contract["schema"] == "magpie.storage-plan.v1"
    assert contract["confirmation_required"] is True
    assert contract["proposal_tool"] == "propose_storage_plan"
    tools = {entry["name"]: entry for entry in contract["write_tools"]}
    assert set(tools) == set(service.write_tool_names)
    assert tools["put_item"]["parameters"]["properties"]["dimensions"]["properties"]
    assert tools["move_container"]["parameters"]["required"] == ["location_id"]
    assert contract["plan_step"]["properties"]["tool"]["enum"] == sorted(tools)
    assert contract["immediate_hardware_tools"] == ["light_location", "clear_lights"]


def test_physical_print_plan_requires_available_printer(tmp_path) -> None:
    service = _service(
        tmp_path,
        printer_status_handler=lambda: {
            "ok": False,
            "error": "printer timed out",
        },
    )

    with pytest.raises(ValueError, match="physical label printer is unavailable"):
        service.propose_plan(
            command="print B001 label",
            steps=[{"tool": "print_label", "arguments": {"location_id": "B001"}}],
        )


def test_light_location_resolves_nearest_lit_binding(tmp_path) -> None:
    calls = []
    service = _service(
        tmp_path,
        light_handler=lambda pixels, color, seconds, target: calls.append(
            (pixels, color, seconds, target)
        ) or True,
    )

    result = service.light_location("B001", color=(255, 0, 0), seconds=30)

    assert result["lit"] is True
    assert result["lit_location"] == "B001"
    assert calls == [([84], (255, 0, 0), 30, "main-cabinet")]


def test_shared_agent_tool_runner_controls_lights(tmp_path) -> None:
    light_calls = []
    clear_calls = []
    service = _service(
        tmp_path,
        light_handler=lambda pixels, color, seconds, target: light_calls.append(
            (pixels, color, seconds, target)
        ) or True,
        clear_lights_handler=lambda: clear_calls.append(True),
    )

    lit = service.read(
        "light_location",
        {"location_id": "B1", "color": "blue", "seconds": 30},
    )
    cleared = service.read("clear_lights", {})

    assert lit["lit"] is True
    assert lit["location_id"] == "B001"
    assert lit["color"] == (40, 120, 255)
    assert light_calls == [([84], (40, 120, 255), 30.0, "main-cabinet")]
    assert cleared == {"ok": True, "message": "Lights cleared."}
    assert clear_calls == [True]
