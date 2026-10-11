import base64
import json
import sqlite3

from fastapi.testclient import TestClient

from searchable_storage_led.api import create_app
from searchable_storage_led.config import LedConfig, ZebraConfig
from searchable_storage_led.locations import LocationNameConflictError
from searchable_storage_led.repositories import StorageRepositories
from searchable_storage_led.store import InventoryStore


def _app(tmp_path, *, zebra: ZebraConfig | None = None) -> TestClient:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [{"size": "small", "bin_count": 2, "spans": [1, 1]}],
            }
        ),
        encoding="utf-8",
    )
    cfg = LedConfig(
        pixel_count=2,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout),
        database_path=str(tmp_path / "inventory.db"),
        zebra=zebra or ZebraConfig(),
    )
    return TestClient(create_app(cfg))


def test_locations_tree_has_cabinet_root_and_bins(tmp_path) -> None:
    client = _app(tmp_path)
    tree = client.get("/locations").json()

    assert tree["roots"] == ["main-cabinet"]
    ids = {loc["id"] for loc in tree["locations"]}
    assert {"main-cabinet", "B001", "B002"} <= ids


def test_location_list_and_detail_bulk_load_assets(tmp_path, monkeypatch) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps(
            {
                "name": "bulk-assets",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]}
                ],
            }
        ),
        encoding="utf-8",
    )
    repositories = StorageRepositories.sqlite(tmp_path / "inventory.db")
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=2,
                backend="wled",
                wled_url="http://127.0.0.1:1",
                layout_path=str(layout),
                database_path=str(tmp_path / "unused.db"),
            ),
            repositories=repositories,
        )
    )
    repositories.locations.set_hardware_binding(
        location_id="B001",
        kind="wled_pixels",
        controller_id="main",
        address="0",
        metadata={"pixels": [0]},
    )
    photo = repositories.inventory.add_photo(
        filename="bin.jpg",
        content_type="image/jpeg",
        data=b"photo",
    )
    repositories.inventory.attach_photo_to_location(
        location_id="B001", photo_id=photo.id
    )

    def reject_per_location_read(*_args, **_kwargs):
        raise AssertionError("location collections must use bulk asset reads")

    monkeypatch.setattr(
        repositories.locations, "hardware_bindings", reject_per_location_read
    )
    monkeypatch.setattr(
        repositories.inventory, "location_photos", reject_per_location_read
    )

    listed = client.get("/locations").json()["locations"]
    listed_bin = next(location for location in listed if location["id"] == "B001")
    detail = client.get("/locations/main-cabinet").json()
    detail_bin = next(child for child in detail["children"] if child["id"] == "B001")

    for location in (listed_bin, detail_bin):
        assert location["hardware_bindings"][0]["address"] == "0"
        assert location["photos"][0]["id"] == photo.id


def test_location_internal_dimensions_round_trip_and_survive_backfill(tmp_path) -> None:
    client = _app(tmp_path)

    updated = client.post(
        "/locations/B001/update",
        json={
            "internal_dimensions": {
                "width_mm": 42,
                "depth_mm": 126,
                "height_mm": 35,
            }
        },
    )
    restarted = _app(tmp_path)
    location = next(
        entry
        for entry in restarted.get("/locations").json()["locations"]
        if entry["id"] == "B001"
    )

    assert updated.status_code == 200
    assert location["internal_dimensions"] == {
        "width_mm": 42.0,
        "depth_mm": 126.0,
        "height_mm": 35.0,
    }


def test_placement_api_excludes_container_that_is_physically_too_small(tmp_path) -> None:
    client = _app(tmp_path)
    client.post(
        "/locations/B001/update",
        json={
            "capacity_class": "large",
            "internal_dimensions": {"width_mm": 40, "depth_mm": 40, "height_mm": 20},
        },
    )
    client.post(
        "/locations/B002/update",
        json={
            "capacity_class": "large",
            "internal_dimensions": {"width_mm": 100, "depth_mm": 60, "height_mm": 30},
        },
    )

    response = client.post(
        "/placement/suggest",
        json={
            "item": "blue pry tool",
            "dimensions": {"length_mm": 75, "width_mm": 20, "height_mm": 10},
        },
    ).json()

    assert "B001" not in {entry["location_id"] for entry in response["suggestions"]}
    assert response["suggestions"][0]["location_id"] == "B002"


def test_location_detail_shows_children_items_and_ancestors(tmp_path) -> None:
    client = _app(tmp_path)
    client.post(
        "/assistant/confirm",
        json={
            "plan": [
                {"tool": "create_location", "arguments": {"type": "pouch", "label": "Blue Pouch", "parent_id": "B001"}},
                {"tool": "put_item", "arguments": {"item": "fuses", "location_id": "blue-pouch", "quantity": 3}},
            ]
        },
    )

    cabinet = client.get("/locations/main-cabinet").json()
    assert cabinet["location"]["child_count"] == 2
    assert {c["id"] for c in cabinet["children"]} == {"B001", "B002"}

    pouch = client.get("/locations/blue-pouch").json()
    assert pouch["location"]["type"] == "pouch"
    assert pouch["ancestors"] == ["B001", "main-cabinet"]
    assert pouch["items"][0]["name"] == "fuses"


def test_unknown_location_returns_404(tmp_path) -> None:
    assert _app(tmp_path).get("/locations/nope").status_code == 404


def test_location_light_color_uses_requested_color(tmp_path, monkeypatch) -> None:
    class MemoryWriter:
        def __init__(self) -> None:
            self.frames = []

        def write_pixels(self, pixels, config) -> None:
            self.frames.append(list(pixels))

    writer = MemoryWriter()
    monkeypatch.setattr(
        "searchable_storage_led.controller.create_writer",
        lambda config: writer,
    )
    client = _app(tmp_path)

    response = client.post(
        "/locations/B001/light-color",
        json={"color": [255, 0, 0], "brightness": 1, "seconds": 120},
    )

    assert response.status_code == 200
    assert response.json()["lit"] is True
    assert response.json()["color"] == [255, 0, 0]
    assert any(pixel == [255, 0, 0] for pixel in client.get("/state").json()["pixels"])


def test_location_light_colors_lights_multiple_destinations(tmp_path, monkeypatch) -> None:
    class MemoryWriter:
        def write_pixels(self, pixels, config) -> None:
            pass

    monkeypatch.setattr(
        "searchable_storage_led.controller.create_writer",
        lambda config: MemoryWriter(),
    )
    client = _app(tmp_path)

    response = client.post(
        "/locations/light-colors",
        json={
            "locations": [
                {"location_id": "B001", "color": [255, 0, 0]},
                {"location_id": "B002", "color": [0, 0, 255]},
            ],
            "brightness": 1,
            "seconds": 120,
        },
    )

    assert response.status_code == 200
    assert response.json()["lit"] is True
    pixels = client.get("/state").json()["pixels"]
    assert [255, 0, 0] in pixels
    assert [0, 0, 255] in pixels


def test_confirmed_putaway_runs_save_pulse_and_reports_effect(tmp_path, monkeypatch) -> None:
    class MemoryWriter:
        def __init__(self) -> None:
            self.frames = []

        def write_pixels(self, pixels, config) -> None:
            self.frames.append(list(pixels))

    writer = MemoryWriter()
    monkeypatch.setattr(
        "searchable_storage_led.controller.create_writer",
        lambda config: writer,
    )
    monkeypatch.setattr("searchable_storage_led.controller.time.sleep", lambda seconds: None)
    client = _app(tmp_path)

    response = client.post(
        "/assistant/confirm",
        json={
            "command": "put away a saved item",
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "saved item",
                        "location_id": "B001",
                        "quantity": 1,
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["lit"] is True
    assert response.json()["lit_location"] == "B001"
    assert response.json()["light_effect"] == "save-pulse"
    assert response.json()["light_seconds"] == 2.0
    assert len(writer.frames) >= 2
    assert any(pixel != (0, 0, 0) for pixel in writer.frames[-1])


def test_manual_putaway_runs_save_pulse_and_reports_effect(tmp_path, monkeypatch) -> None:
    class MemoryWriter:
        def __init__(self) -> None:
            self.frames = []

        def write_pixels(self, pixels, config) -> None:
            self.frames.append(list(pixels))

    writer = MemoryWriter()
    monkeypatch.setattr(
        "searchable_storage_led.controller.create_writer",
        lambda config: writer,
    )
    monkeypatch.setattr("searchable_storage_led.controller.time.sleep", lambda seconds: None)
    client = _app(tmp_path)

    response = client.post(
        "/inventory",
        json={"name": "checked item", "bin_id": "B001", "quantity": 1},
    )

    assert response.status_code == 200
    assert response.json()["lit"] is True
    assert response.json()["lit_location"] == "B001"
    assert response.json()["light_effect"] == "save-pulse"
    assert response.json()["light_seconds"] == 2.0
    assert len(writer.frames) >= 2
    assert any(pixel != (0, 0, 0) for pixel in writer.frames[-1])


def test_cabinet_flash_uses_authoritative_location_binding(tmp_path) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps(
            {
                "name": "test",
                "columns": 2,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 2, "spans": [1, 1]}
                ],
            }
        ),
        encoding="utf-8",
    )
    repositories = StorageRepositories.sqlite(tmp_path / "inventory.db")
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=2,
                backend="none",
                layout_path=str(layout),
                database_path=str(tmp_path / "unused.db"),
            ),
            repositories=repositories,
        )
    )
    repositories.locations.set_hardware_binding(
        location_id="B001",
        kind="wled_pixels",
        controller_id="main-cabinet",
        address="1",
        metadata={"pixels": [1]},
    )

    response = client.post(
        "/bins/B001/flash",
        json={"color": [255, 255, 255], "brightness": 1, "seconds": 30},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "bin_id": "B001",
        "lit": True,
        "lit_location": "B001",
        "light_target": "main-cabinet",
        "led_indexes": [1],
        "seconds": 30.0,
    }
    assert client.get("/state").json()["pixels"] == [
        [0, 0, 0],
        [255, 255, 255],
    ]


def test_move_inventory_between_bins(tmp_path) -> None:
    client = _app(tmp_path)
    added = client.post("/inventory", json={"name": "M3 screws", "bin_id": "B001", "quantity": 5})
    item_id = added.json()["item_id"]

    moved = client.post(
        "/inventory/move", json={"item_id": item_id, "from_bin": "B001", "to_bin": "B002"}
    )
    assert moved.status_code == 200
    assert moved.json()["to_bin"] == "B002"
    assert moved.json()["interaction_id"] > 0

    assert client.get("/locations/B001").json()["items"] == []
    landed = client.get("/locations/B002").json()["items"]
    assert landed[0]["name"] == "M3 screws" and landed[0]["quantity"] == 5
    interaction = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert interaction["id"] == moved.json()["interaction_id"]
    assert interaction["route"] == "/inventory/manual"
    assert interaction["status"] == "manual"
    assert interaction["response"]["action"] == "manual_move_inventory"
    assert interaction["response"]["applied"][0]["moved_item"] == item_id
    example = InventoryStore(tmp_path / "inventory.db").recent_examples(limit=1)[0]
    assert example["kind"] == "manual"
    assert example["command"] == f"manual move item {item_id} from B001 to B002"
    assert json.loads(example["action"])[0]["tool"] == "move_item"

    undone = client.post("/assistant/undo").json()
    assert undone["ok"] is True
    assert client.get("/locations/B001").json()["items"][0]["quantity"] == 5
    assert client.get("/locations/B002").json()["items"] == []


def test_move_inventory_merges_quantities(tmp_path) -> None:
    client = _app(tmp_path)
    a = client.post("/inventory", json={"name": "washers", "bin_id": "B001", "quantity": 4}).json()
    client.post("/inventory", json={"name": "washers", "bin_id": "B002", "quantity": 6})

    client.post(
        "/inventory/move", json={"item_id": a["item_id"], "from_bin": "B001", "to_bin": "B002"}
    )

    landed = client.get("/locations/B002").json()["items"]
    assert len(landed) == 1 and landed[0]["quantity"] == 10
    assert client.get("/locations/B001").json()["items"] == []

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    assert client.get("/locations/B001").json()["items"][0]["quantity"] == 4
    assert client.get("/locations/B002").json()["items"][0]["quantity"] == 6


def test_inventory_move_preview_reports_merge_without_mutating_inventory(
    tmp_path,
) -> None:
    client = _app(tmp_path)
    item = client.post(
        "/inventory",
        json={"name": "washers", "bin_id": "B001", "quantity": 4},
    ).json()
    client.post(
        "/inventory",
        json={"name": "washers", "bin_id": "B002", "quantity": 6},
    )

    response = client.post(
        "/inventory/move-preview",
        json={
            "entries": [{"item_id": item["item_id"], "from_bin": "B001"}],
            "to_bin": "B002",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["to_bin"] == "B002"
    assert body["destination"]["path"][-1]["id"] == "B002"
    assert body["selected_count"] == 1
    assert body["move_count"] == 1
    assert body["already_there_count"] == 0
    assert body["item_count"] == 1
    assert body["source_location_count"] == 1
    assert body["merge_count"] == 1
    assert body["review_resolution_count"] == 0
    assert body["entries"] == [
        {
            "item_id": item["item_id"],
            "name": "washers",
            "from_location_id": "B001",
            "quantity": 4.0,
            "unit": "each",
            "combines_with_existing": True,
            "resolves_review": False,
        }
    ]
    assert client.get("/locations/B001").json()["items"][0]["quantity"] == 4
    assert client.get("/locations/B002").json()["items"][0]["quantity"] == 6


def test_bulk_inventory_move_preview_counts_placements_already_at_destination(
    tmp_path,
) -> None:
    client = _app(tmp_path)
    screws = client.post(
        "/inventory",
        json={"name": "M3 screws", "bin_id": "B001", "quantity": 5},
    ).json()
    washers = client.post(
        "/inventory",
        json={"name": "M3 washers", "bin_id": "B002", "quantity": 8},
    ).json()

    response = client.post(
        "/inventory/move-preview",
        json={
            "entries": [
                {"item_id": screws["item_id"], "from_bin": "B001"},
                {"item_id": washers["item_id"], "from_bin": "B002"},
            ],
            "to_bin": "B002",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["selected_count"] == 2
    assert body["move_count"] == 1
    assert body["already_there_count"] == 1
    assert body["item_count"] == 2
    assert body["source_location_count"] == 2
    assert body["merge_count"] == 0
    assert [entry["name"] for entry in body["entries"]] == ["M3 screws"]
    assert client.get("/locations/B001").json()["items"][0]["name"] == "M3 screws"
    assert client.get("/locations/B002").json()["items"][0]["name"] == "M3 washers"


def test_bulk_move_of_multiple_placements_for_one_item_is_exactly_undoable(
    tmp_path,
) -> None:
    client = _app(tmp_path)
    item = client.post(
        "/inventory",
        json={"name": "washers", "bin_id": "B001", "quantity": 2},
    ).json()
    client.post(
        "/inventory",
        json={"name": "washers", "bin_id": "B002", "quantity": 3},
    )
    client.post(
        "/locations",
        json={"type": "box", "label": "Assembly Cart"},
    )
    request = {
        "entries": [
            {"item_id": item["item_id"], "from_bin": "B001"},
            {"item_id": item["item_id"], "from_bin": "B002"},
        ],
        "to_bin": "assembly-cart",
    }

    preview = client.post("/inventory/move-preview", json=request).json()
    moved = client.post("/inventory/move-bulk", json=request)

    assert preview["item_count"] == 1
    assert preview["move_count"] == 2
    assert preview["merge_count"] == 1
    assert [
        entry["combines_with_existing"] for entry in preview["entries"]
    ] == [False, True]
    assert moved.status_code == 200
    assert client.get("/locations/assembly-cart").json()["items"][0]["quantity"] == 5

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    assert client.get("/locations/B001").json()["items"][0]["quantity"] == 2
    assert client.get("/locations/B002").json()["items"][0]["quantity"] == 3
    assert client.get("/locations/assembly-cart").json()["items"] == []


def test_bulk_move_inventory_records_one_undoable_operation(tmp_path) -> None:
    client = _app(tmp_path)
    screws = client.post(
        "/inventory", json={"name": "M3 screws", "bin_id": "B001", "quantity": 5}
    ).json()
    washers = client.post(
        "/inventory", json={"name": "M3 washers", "bin_id": "B001", "quantity": 8}
    ).json()

    moved = client.post(
        "/inventory/move-bulk",
        json={
            "entries": [
                {"item_id": screws["item_id"], "from_bin": "B001"},
                {"item_id": washers["item_id"], "from_bin": "B001"},
            ],
            "to_bin": "B002",
        },
    )

    assert moved.status_code == 200
    assert moved.json()["moved_count"] == 2
    assert moved.json()["interaction_id"] > 0
    assert {item["name"] for item in client.get("/locations/B002").json()["items"]} == {
        "M3 screws",
        "M3 washers",
    }
    interaction = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert interaction["route"] == "/inventory/manual"
    assert interaction["response"]["action"] == "manual_bulk_move_inventory"
    assert len(interaction["response"]["applied"]) == 2

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    restored = client.get("/locations/B001").json()["items"]
    assert {item["name"]: item["quantity"] for item in restored} == {
        "M3 screws": 5,
        "M3 washers": 8,
    }
    assert client.get("/locations/B002").json()["items"] == []


def test_bulk_move_inventory_validates_every_source_before_moving(tmp_path) -> None:
    client = _app(tmp_path)
    screws = client.post(
        "/inventory", json={"name": "M3 screws", "bin_id": "B001", "quantity": 5}
    ).json()

    moved = client.post(
        "/inventory/move-bulk",
        json={
            "entries": [
                {"item_id": screws["item_id"], "from_bin": "B001"},
                {"item_id": 999999, "from_bin": "B001"},
            ],
            "to_bin": "B002",
        },
    )

    assert moved.status_code == 409
    assert client.get("/locations/B001").json()["items"][0]["quantity"] == 5
    assert client.get("/locations/B002").json()["items"] == []


def test_bulk_move_inventory_restores_completed_moves_after_failure(
    tmp_path, monkeypatch
) -> None:
    original_move = InventoryStore.move_inventory
    calls = 0

    def fail_second_move(self, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return False
        return original_move(self, **kwargs)

    monkeypatch.setattr(InventoryStore, "move_inventory", fail_second_move)
    client = _app(tmp_path)
    screws = client.post(
        "/inventory", json={"name": "M3 screws", "bin_id": "B001", "quantity": 5}
    ).json()
    washers = client.post(
        "/inventory", json={"name": "M3 washers", "bin_id": "B001", "quantity": 8}
    ).json()

    moved = client.post(
        "/inventory/move-bulk",
        json={
            "entries": [
                {"item_id": screws["item_id"], "from_bin": "B001"},
                {"item_id": washers["item_id"], "from_bin": "B001"},
            ],
            "to_bin": "B002",
        },
    )

    assert moved.status_code == 409
    restored = client.get("/locations/B001").json()["items"]
    assert {item["name"]: item["quantity"] for item in restored} == {
        "M3 screws": 5,
        "M3 washers": 8,
    }
    assert client.get("/locations/B002").json()["items"] == []


def test_bulk_move_inventory_skips_selected_placement_already_at_destination(tmp_path) -> None:
    client = _app(tmp_path)
    screws = client.post(
        "/inventory", json={"name": "M3 screws", "bin_id": "B001", "quantity": 5}
    ).json()
    washers = client.post(
        "/inventory", json={"name": "M3 washers", "bin_id": "B002", "quantity": 8}
    ).json()

    moved = client.post(
        "/inventory/move-bulk",
        json={
            "entries": [
                {"item_id": screws["item_id"], "from_bin": "B001"},
                {"item_id": washers["item_id"], "from_bin": "B002"},
            ],
            "to_bin": "B002",
        },
    )

    assert moved.status_code == 200
    assert moved.json()["moved_count"] == 1
    assert {item["name"] for item in client.get("/locations/B002").json()["items"]} == {
        "M3 screws",
        "M3 washers",
    }


def test_move_inventory_unknown_destination_400(tmp_path) -> None:
    client = _app(tmp_path)
    added = client.post("/inventory", json={"name": "nuts", "bin_id": "B001", "quantity": 1})
    resp = client.post(
        "/inventory/move",
        json={"item_id": added.json()["item_id"], "from_bin": "B001", "to_bin": "nope"},
    )
    assert resp.status_code == 400


def test_remove_inventory_from_loose_location_only(tmp_path) -> None:
    client = _app(tmp_path)
    client.post(
        "/locations",
        json={
            "type": "room",
            "label": "Attic",
            "parent_id": None,
            "accepts_items": True,
        },
    )
    added = client.post("/inventory", json={"name": "T8x8 lead screw nut", "bin_id": "B001", "quantity": 2}).json()
    client.post("/inventory", json={"name": "T8x8 lead screw nut", "bin_id": "attic", "quantity": 2})

    removed = client.delete(f"/inventory/{added['item_id']}/locations/attic")

    assert removed.status_code == 200
    assert removed.json()["interaction_id"] > 0
    assert client.get("/locations/attic").json()["items"] == []
    assert client.get("/locations/B001").json()["items"][0]["name"] == "T8x8 lead screw nut"
    interaction = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert interaction["id"] == removed.json()["interaction_id"]
    assert interaction["response"]["action"] == "manual_remove_inventory"
    assert interaction["response"]["applied"][0]["quantity"] == 2

    undone = client.post("/assistant/undo").json()
    assert undone["ok"] is True
    assert client.get("/locations/attic").json()["items"][0]["quantity"] == 2


def test_remove_inventory_accepts_legacy_cabinet_bin_id(tmp_path) -> None:
    client = _app(tmp_path)
    added = client.post("/inventory", json={"name": "flanged brass nut", "bin_id": "1", "quantity": 2}).json()

    removed = client.delete(f"/inventory/{added['item_id']}/locations/1")

    assert removed.status_code == 200
    assert removed.json()["bin_id"] == "B001"
    assert client.get("/locations/B001").json()["items"] == []


def test_create_location_and_add_item_from_ui_path(tmp_path) -> None:
    client = _app(tmp_path)

    created = client.post(
        "/locations",
        json={
            "type": "drawer",
            "label": "Drawer 3",
            "parent_id": None,
            "capacity_class": "large",
            "purpose_tags": ["printer", "tools"],
            "placement_notes": "Large printer tools and parts.",
        },
    )
    assert created.status_code == 200
    assert created.json()["id"] == "drawer-3"

    added = client.post(
        "/locations/drawer-3/items", json={"name": "feeler gauges", "quantity": 1}
    )
    assert added.status_code == 200
    assert added.json()["interaction_id"] > 0

    detail = client.get("/locations/drawer-3").json()
    assert detail["location"]["type"] == "drawer"
    assert detail["location"]["capacity_class"] == "large"
    assert detail["location"]["purpose_tags"] == ["printer", "tools"]
    assert "printer tools" in detail["location"]["placement_notes"]
    assert detail["items"][0]["name"] == "feeler gauges"
    interaction = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert interaction["id"] == added.json()["interaction_id"]
    assert interaction["route"] == "/inventory/manual"
    assert interaction["response"]["action"] == "manual_location_add_item"
    example = InventoryStore(tmp_path / "inventory.db").recent_examples(limit=1)[0]
    assert example["kind"] == "manual"
    assert example["command"] == "manual add feeler gauges to drawer-3"
    assert json.loads(example["action"])[0]["tool"] == "put_item"


def test_location_creation_rejects_duplicate_sibling_names(tmp_path) -> None:
    client = _app(tmp_path)
    client.post(
        "/locations",
        json={"type": "room", "label": "Garage", "parent_id": None},
    )
    first = client.post(
        "/locations",
        json={"type": "cabinet", "label": "Parts Cabinet", "parent_id": "garage"},
    )

    duplicate = client.post(
        "/locations",
        json={"type": "shelf", "label": "  parts   cabinet  ", "parent_id": "garage"},
    )
    same_name_elsewhere = client.post(
        "/locations",
        json={"type": "cabinet", "label": "Parts Cabinet", "parent_id": None},
    )

    assert first.status_code == 200
    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["detail"]
    assert same_name_elsewhere.status_code == 200
    garage_children = client.get("/locations/garage").json()["children"]
    assert [child["label"] for child in garage_children] == ["Parts Cabinet"]


def test_location_database_race_returns_conflict(tmp_path, monkeypatch) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
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
    repositories = StorageRepositories.sqlite(tmp_path / "inventory.db")
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="wled",
                wled_url="http://127.0.0.1:1",
                layout_path=str(layout),
                database_path=str(tmp_path / "unused.db"),
            ),
            repositories=repositories,
        )
    )

    def reject_duplicate(_location):
        raise LocationNameConflictError(
            "A storage place with that name already exists at this level."
        )

    monkeypatch.setattr(repositories.locations, "upsert", reject_duplicate)

    response = client.post(
        "/locations",
        json={"type": "room", "label": "Garage"},
    )

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_structure_and_batch_creation_preflight_duplicate_names(tmp_path) -> None:
    client = _app(tmp_path)
    client.post(
        "/locations",
        json={"type": "room", "label": "Workshop", "parent_id": None},
    )
    client.post(
        "/locations",
        json={"type": "cabinet", "label": "Tool Wall", "parent_id": "workshop"},
    )
    client.post(
        "/locations",
        json={"type": "shelf", "label": "Shelf 2", "parent_id": "tool-wall"},
    )

    duplicate_structure = client.post(
        "/locations/structure",
        json={
            "label": "Tool Wall",
            "type": "cabinet",
            "parent_id": "workshop",
            "child_count": 2,
            "child_type": "drawer",
            "child_label_prefix": "Drawer",
        },
    )
    duplicate_batch = client.post(
        "/locations/tool-wall/children/batch",
        json={
            "labels": ["Shelf 1", "shelf 2"],
            "type": "shelf",
        },
    )

    assert duplicate_structure.status_code == 409
    assert duplicate_batch.status_code == 409
    children = client.get("/locations/tool-wall").json()["children"]
    assert [child["label"] for child in children] == ["Shelf 2"]


def test_location_rename_move_and_restore_reject_ambiguous_siblings(tmp_path) -> None:
    client = _app(tmp_path)
    for label in ("Garage", "Attic"):
        client.post(
            "/locations",
            json={"type": "room", "label": label, "parent_id": None},
        )
    client.post(
        "/locations",
        json={
            "id": "garage-fasteners",
            "type": "box",
            "label": "Fasteners",
            "parent_id": "garage",
        },
    )
    client.post(
        "/locations",
        json={
            "id": "attic-fasteners",
            "type": "box",
            "label": "Fasteners",
            "parent_id": "attic",
        },
    )
    client.post(
        "/locations",
        json={"type": "box", "label": "Electrical", "parent_id": "attic"},
    )

    rename = client.post(
        "/locations/electrical/update",
        json={"label": " fasteners "},
    )
    move_preview = client.get(
        "/locations/garage-fasteners/move-preview?new_parent_id=attic"
    )

    assert rename.status_code == 409
    assert move_preview.status_code == 409
    assert client.get("/locations/electrical").json()["location"]["label"] == "Electrical"
    assert (
        client.get("/locations/garage-fasteners").json()["location"]["parent_id"]
        == "garage"
    )

    with sqlite3.connect(tmp_path / "inventory.db") as connection:
        connection.execute(
            """
            INSERT INTO locations (
                id, parent_id, type, label, capacity_class, accepts_items,
                active, archived_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-attic-fasteners",
                "attic",
                "box",
                "Fasteners",
                "medium",
                1,
                0,
                "2026-07-28T12:00:00+00:00",
            ),
        )

    restore = client.post("/locations/legacy-attic-fasteners/restore")

    assert restore.status_code == 409
    archived = next(
        location
        for location in client.get("/locations?include_archived=true").json()[
            "locations"
        ]
        if location["id"] == "legacy-attic-fasteners"
    )
    assert archived["active"] is False


def test_inventory_post_accepts_non_cabinet_location_without_mangling(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "drawer", "label": "Drawer 2", "parent_id": None})
    client.post("/locations", json={"type": "bin", "label": "D2 one", "parent_id": "drawer-2"})

    # The Add/Update form posts here; lowercase/hyphen ids must NOT be normalized.
    resp = client.post(
        "/inventory",
        json={"name": "digital calipers", "bin_id": "d2-one", "quantity": 1, "unit": "each"},
    )
    assert resp.status_code == 200
    assert resp.json()["bin_id"] == "d2-one"  # stored verbatim, not "D2-ONE"
    assert resp.json()["interaction_id"] > 0

    detail = client.get("/locations/d2-one").json()
    assert detail["items"][0]["name"] == "digital calipers"
    interaction = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert interaction["id"] == resp.json()["interaction_id"]
    assert interaction["response"]["action"] == "manual_put_inventory"
    example = InventoryStore(tmp_path / "inventory.db").recent_examples(limit=1)[0]
    assert example["kind"] == "manual"
    assert example["command"] == "manual put digital calipers in d2-one"
    assert json.loads(example["action"])[0]["tool"] == "put_item"

    undone = client.post("/assistant/undo").json()
    assert undone["ok"] is True
    assert client.get("/locations/d2-one").json()["items"] == []


def test_inventory_post_still_accepts_legacy_cabinet_bin(tmp_path) -> None:
    client = _app(tmp_path)
    resp = client.post("/inventory", json={"name": "bolt", "bin_id": "1", "quantity": 1})
    assert resp.status_code == 200
    assert resp.json()["bin_id"] == "B001"


def test_fill_grid_generates_positioned_cells(tmp_path) -> None:
    client = _app(tmp_path)
    client.post(
        "/locations",
        json={"type": "drawer", "label": "Drawer 5", "parent_id": None, "cols": 3, "rows": 2},
    )

    filled = client.post("/locations/drawer-5/fill", json={})
    assert filled.status_code == 200
    assert filled.json()["created"] == 6

    detail = client.get("/locations/drawer-5").json()
    assert detail["location"]["child_count"] == 6
    positions = {(c["col"], c["row"]) for c in detail["children"]}
    assert (1, 1) in positions and (3, 2) in positions


def test_fill_with_coords_naming_uses_coordinates(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "drawer", "label": "D", "parent_id": None, "cols": 2, "rows": 1})
    client.post("/locations/d/fill", json={"naming": "coords"})
    labels = {c["label"] for c in client.get("/locations/d").json()["children"]}
    assert labels == {"C1·R1", "C2·R1"}


def test_location_detail_includes_labelled_path(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "cabinet", "label": "Chest", "parent_id": None})
    client.post("/locations", json={"type": "drawer", "label": "Drawer A", "parent_id": "chest"})
    path = [p["label"] for p in client.get("/locations/drawer-a").json()["path"]]
    assert path == ["Chest", "Drawer A"]


def test_create_single_cell_on_demand_is_parent_scoped(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "drawer", "label": "Drawer 7", "parent_id": None, "cols": 4, "rows": 4})

    created = client.post("/locations/drawer-7/cell", json={"col": 2, "row": 3})
    assert created.status_code == 200
    assert created.json()["id"] == "drawer-7-c2-r3"

    cell = next(c for c in client.get("/locations/drawer-7").json()["children"] if c["id"] == "drawer-7-c2-r3")
    assert cell["col"] == 2 and cell["row"] == 3


def test_create_drawer_with_grid_is_returned_for_diagram_layout(tmp_path) -> None:
    client = _app(tmp_path)
    client.post(
        "/locations",
        json={"type": "drawer", "label": "Drawer 9", "parent_id": None, "cols": 7, "rows": 9},
    )
    loc = client.get("/locations/drawer-9").json()["location"]
    assert loc["grid"] == {"cols": 7, "rows": 9}


def test_location_contents_aggregates_subtree_items(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "drawer", "label": "Drawer 8", "parent_id": None, "cols": 2, "rows": 2})
    client.post("/locations/drawer-8/fill", json={})
    client.post("/inventory", json={"name": "o-rings", "bin_id": "drawer-8-c1-r1", "quantity": 5})

    detail = client.get("/locations/drawer-8").json()
    assert detail["items"] == []  # nothing stored directly in the drawer
    contents = {c["name"]: c["bin_id"] for c in detail["contents"]}
    assert contents["o-rings"] == "drawer-8-c1-r1"  # found in a bin inside it


def test_location_contents_can_be_paged_and_filtered_without_full_detail(tmp_path) -> None:
    client = _app(tmp_path)
    client.post(
        "/locations",
        json={"type": "drawer", "label": "Fasteners", "parent_id": None},
    )
    for index in range(35):
        client.post(
            "/locations/fasteners/items",
            json={"name": f"M4 washer {index:02d}", "quantity": 1},
        )

    first = client.get("/locations/fasteners/contents?limit=10").json()
    second = client.get("/locations/fasteners/contents?limit=10&offset=10").json()
    filtered = client.get(
        "/locations/fasteners/contents?limit=100&q=washer%203"
    ).json()
    compact = client.get(
        "/locations/fasteners?include_children=false&include_contents=false"
    ).json()

    assert first["schema"] == "magpie.location_contents.v1"
    assert first["total"] == 35
    assert first["has_more"] is True
    assert len(first["items"]) == 10
    assert len(second["items"]) == 10
    assert {item["item_id"] for item in first["items"]}.isdisjoint(
        {item["item_id"] for item in second["items"]}
    )
    assert filtered["total"] == 5
    assert all("washer 3" in item["name"].lower() for item in filtered["items"])
    assert "children" not in compact
    assert "contents" not in compact
    assert compact["location"]["id"] == "fasteners"


def test_location_contents_verification_persists_subtree_evidence(tmp_path) -> None:
    client = _app(tmp_path)
    client.post(
        "/locations",
        json={"type": "drawer", "label": "Fasteners", "parent_id": None},
    )
    client.post(
        "/locations",
        json={"type": "bin", "label": "Metric", "parent_id": "fasteners"},
    )
    washers = client.post(
        "/locations/fasteners/items",
        json={"name": "M4 washers", "quantity": 20},
    ).json()
    t_nuts = client.post(
        "/locations/metric/items",
        json={"name": "M4 T-nuts", "quantity": 8},
    ).json()
    InventoryStore(tmp_path / "inventory.db").mark_needs_review(
        item_id=washers["item_id"],
        bin_id="fasteners",
        reason="Confirm washer finish.",
    )

    assert client.get("/locations/fasteners").json()["verification"] is None
    audit = client.get("/locations/fasteners/audit").json()
    assert audit["schema"] == "magpie.location_audit.v1"
    assert audit["can_verify"] is True
    assert audit["placement_count"] == 2
    assert audit["item_count"] == 2
    assert audit["location_count"] == 2
    assert set(audit["content_revisions"]) == {"fasteners", "metric"}
    assert len(audit["revision"]) == 64
    by_item = {row["item_id"]: row for row in audit["placements"]}
    assert by_item[washers["item_id"]]["location_path_label"] == "Fasteners"
    assert by_item[t_nuts["item_id"]]["location_path_label"] == "Fasteners › Metric"

    verified = client.post(
        "/locations/fasteners/verify",
        json={
            "revision": audit["revision"],
            "placements": [
                {
                    "item_id": washers["item_id"],
                    "location_id": "fasteners",
                    "status": "present",
                },
                {
                    "item_id": t_nuts["item_id"],
                    "location_id": "metric",
                    "status": "missing",
                },
            ],
        },
    )

    assert verified.status_code == 200
    body = verified.json()
    assert body["schema"] == "magpie.location_verification.v2"
    assert body["verification"]["item_count"] == 2
    assert body["verification"]["placement_count"] == 2
    assert body["verification"]["location_count"] == 2
    assert body["verification"]["location_ids"] == ["fasteners", "metric"]
    assert body["verification"]["includes_nested"] is True
    assert body["verification"]["reviewed_count"] == 2
    assert body["verification"]["present_count"] == 1
    assert body["verification"]["missing_count"] == 1
    assert body["verification"]["verified_at"]
    assert body["verification"]["content_revision"] == audit["content_revisions"]["fasteners"]
    assert body["missing_count"] == 1
    assert body["resolved_review_count"] == 1

    persisted = client.get("/locations/fasteners").json()["verification"]
    assert persisted["placement_count"] == 2
    assert persisted["note"] == "2 placements reviewed; 1 missing"
    assert persisted["missing_count"] == 1
    nested_verification = client.get("/locations/metric").json()["verification"]
    assert nested_verification["root_location_id"] == "fasteners"
    assert nested_verification["placement_count"] == 1
    assert nested_verification["item_count"] == 1
    assert nested_verification["missing_count"] == 1
    assert nested_verification["content_revision"] == audit["content_revisions"]["metric"]
    assert nested_verification["note"] == "1 placement reviewed; 1 missing"
    [review] = client.get("/inventory/review").json()["items"]
    assert review["item_id"] == t_nuts["item_id"]
    assert review["bin_id"] == "metric"
    assert review["review_reason"] == (
        "Reported missing during contents check of Fasteners."
    )
    history = client.get("/locations/fasteners/history").json()["events"]
    assert history[0]["event_type"] == "location_contents_verified"
    evidence = json.loads(history[0]["note"])
    assert evidence["schema"] == "magpie.location_verification.v2"
    assert evidence["missing"] == [
        {"item_id": t_nuts["item_id"], "location_id": "metric"}
    ]
    assert evidence["location_summaries"]["fasteners"]["placement_count"] == 1
    assert evidence["location_summaries"]["metric"]["missing_count"] == 1

    scan = client.get("/l/fasteners")
    assert 'id="verificationStatus"' in scan.text
    assert 'id="verifyContents"' in scan.text
    assert "/storage?location=fasteners&amp;check=1" in scan.text
    assert "Check contents" in scan.text

    assert client.get("/locations/missing/audit").status_code == 404


def test_contents_verification_rejects_stale_or_incomplete_checklists(tmp_path) -> None:
    client = _app(tmp_path)
    client.post(
        "/locations",
        json={"type": "drawer", "label": "Fasteners", "parent_id": None},
    )
    stored = client.post(
        "/locations/fasteners/items",
        json={"name": "M4 washers", "quantity": 20},
    ).json()
    audit = client.get("/locations/fasteners/audit").json()

    incomplete = client.post(
        "/locations/fasteners/verify",
        json={"revision": audit["revision"], "placements": []},
    )
    assert incomplete.status_code == 400
    assert "account for every expected placement" in incomplete.json()["detail"]

    client.post(
        f"/inventory/{stored['item_id']}",
        json={
            "name": "M4 washers",
            "bin_id": "fasteners",
            "quantity": 24,
            "unit": "each",
        },
    )
    stale = client.post(
        "/locations/fasteners/verify",
        json={
            "revision": audit["revision"],
            "placements": [
                {
                    "item_id": stored["item_id"],
                    "location_id": "fasteners",
                    "status": "present",
                }
            ],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "contents_check_stale"
    assert client.get("/locations/fasteners").json()["verification"] is None


def test_empty_location_can_be_explicitly_verified(tmp_path) -> None:
    client = _app(tmp_path)
    client.post(
        "/locations",
        json={"type": "drawer", "label": "Empty drawer", "parent_id": None},
    )
    audit = client.get("/locations/empty-drawer/audit").json()
    assert audit["placements"] == []
    assert audit["can_verify"] is True

    checked = client.post(
        "/locations/empty-drawer/verify",
        json={"revision": audit["revision"], "placements": []},
    )
    assert checked.status_code == 200
    assert checked.json()["verification"]["reviewed_count"] == 0
    assert checked.json()["verification"]["note"] == "0 placements reviewed; all present"


def test_location_confidence_tracks_checks_age_and_missing_reports(tmp_path) -> None:
    client = _app(tmp_path)
    stored = client.post(
        "/inventory",
        json={"name": "M4 T-nuts", "bin_id": "B001", "quantity": 24},
    ).json()

    listed = client.get("/locations").json()
    by_id = {location["id"]: location for location in listed["locations"]}
    assert by_id["B001"]["confidence"] == {
        "state": "unchecked",
        "label": "Contents not checked",
        "item_count": 1,
        "review_count": 0,
        "content_revision": 1,
        "verified_at": None,
    }
    assert by_id["B002"]["confidence"]["state"] == "empty"

    audit = client.get("/locations/B001/audit").json()
    client.post(
        "/locations/B001/verify",
        json={
            "revision": audit["revision"],
            "placements": [
                {
                    "item_id": stored["item_id"],
                    "location_id": "B001",
                    "status": "present",
                }
            ],
        },
    )
    checked = next(
        location
        for location in client.get("/locations").json()["locations"]
        if location["id"] == "B001"
    )
    assert checked["confidence"]["state"] == "checked"
    assert checked["confidence"]["verified_at"]

    with sqlite3.connect(tmp_path / "inventory.db") as conn:
        conn.execute(
            """
            UPDATE events
            SET created_at = '2020-01-01 00:00:00'
            WHERE event_type = 'location_contents_verified' AND bin_id = 'B001'
            """
        )
    stale = next(
        location
        for location in client.get("/locations").json()["locations"]
        if location["id"] == "B001"
    )
    assert stale["confidence"]["state"] == "stale"

    found = client.post("/assistant/find", json={"query": "M4 T-nuts"}).json()
    correction = client.post(
        "/assistant/find/not-here",
        json={
            "item_id": stored["item_id"],
            "location_id": "B001",
            "query": "M4 T-nuts",
            "active_find_id": found["active_finds"][0]["id"],
        },
    )
    assert correction.status_code == 200
    contradicted = client.get("/locations/B001").json()
    assert contradicted["confidence"]["state"] == "attention"
    assert contradicted["confidence"]["review_count"] == 1
    assert contradicted["location"]["review_count"] == 1


def test_location_tree_returns_nested_grids(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "cabinet", "label": "Chest", "parent_id": None, "cols": 1, "rows": 2})
    client.post("/locations", json={"type": "drawer", "label": "D1", "parent_id": "chest", "cols": 2, "rows": 2, "col": 1, "row": 1})
    client.post("/locations/d1/fill", json={})

    tree = client.get("/locations/chest/tree").json()
    assert tree["grid"] == {"cols": 1, "rows": 2}
    d1 = next(c for c in tree["children"] if c["id"] == "d1")
    assert d1["grid"] == {"cols": 2, "rows": 2}
    assert len(d1["children"]) == 4  # the drawer's cells nested under it


def test_update_location_changes_label_type_and_grid(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "drawer", "label": "D", "parent_id": None})

    resp = client.post("/locations/d/update", json={"label": "Big Drawer", "cols": 3, "rows": 5})
    assert resp.status_code == 200

    loc = client.get("/locations/d").json()["location"]
    assert loc["label"] == "Big Drawer"
    assert loc["grid"] == {"cols": 3, "rows": 5}


def test_update_location_changes_placement_metadata(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "pouch", "label": "Solder Pouch", "parent_id": None})

    resp = client.post(
        "/locations/solder-pouch/update",
        json={
            "capacity_class": "medium",
            "purpose_tags": ["solder", "electronics"],
            "placement_notes": "Solder, wick, flux, tips, and compact soldering tools.",
        },
    )

    assert resp.status_code == 200
    loc = client.get("/locations/solder-pouch").json()["location"]
    assert loc["capacity_class"] == "medium"
    assert loc["purpose_tags"] == ["solder", "electronics"]
    assert "wick" in loc["placement_notes"]


def test_placement_suggest_endpoint_prefers_matching_pouch(tmp_path) -> None:
    client = _app(tmp_path)
    client.post(
        "/locations",
        json={
            "type": "pouch",
            "label": "Solder Pouch",
            "capacity_class": "medium",
            "purpose_tags": ["solder", "electronics"],
        },
    )

    resp = client.post("/placement/suggest", json={"item": "solder wick"})

    assert resp.status_code == 200
    assert resp.json()["suggestions"][0]["location_id"] == "solder-pouch"


def test_location_label_zpl_includes_subtree_contents(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "room", "label": "Attic", "capacity_class": "oversize"})
    client.post(
        "/locations",
        json={
            "type": "box",
            "label": "Roaster Parts Bin",
            "parent_id": "attic",
            "capacity_class": "large",
            "purpose_tags": ["roaster", "parts", "spares"],
        },
    )
    client.post("/locations/roaster-parts-bin/items", json={"name": "spare fans"})
    client.post("/locations/roaster-parts-bin/items", json={"name": "spare burner"})

    resp = client.get("/locations/roaster-parts-bin/label.zpl")

    assert resp.status_code == 200
    assert "ROASTER PARTS BIN" in resp.text
    assert "Attic" in resp.text
    assert "spare fans" in resp.text
    assert "^BQN" in resp.text
    assert "https://magpie.example.com/scan/local/l/roaster-parts-bin" in resp.text


def test_location_label_sheet_includes_printable_qr_cards(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "room", "label": "Attic", "capacity_class": "oversize"})
    client.post(
        "/locations",
        json={
            "type": "box",
            "label": "Roaster Parts Bin",
            "parent_id": "attic",
            "capacity_class": "large",
            "purpose_tags": ["roaster", "parts"],
        },
    )
    client.post("/locations/roaster-parts-bin/items", json={"name": "spare fans", "quantity": 2})

    resp = client.get("/locations/attic/labels.html")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Attic labels" in resp.text
    assert "Roaster Parts Bin" in resp.text
    assert "spare fans" in resp.text
    assert "Print sheet" in resp.text
    assert 'href="/locations/attic/labels.pdf"' in resp.text
    assert "Export PDF" in resp.text
    assert "http://magpie.example.com:8765/l/roaster-parts-bin" not in resp.text
    assert "<svg" in resp.text
    assert "QR code for Roaster Parts Bin" in resp.text


def test_location_label_sheet_pdf_includes_printable_qr_cards(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "room", "label": "Attic", "capacity_class": "oversize"})
    client.post(
        "/locations",
        json={
            "type": "box",
            "label": "Roaster Parts Bin",
            "parent_id": "attic",
            "capacity_class": "large",
            "purpose_tags": ["roaster", "parts"],
        },
    )
    client.post("/locations/roaster-parts-bin/items", json={"name": "spare fans", "quantity": 2})

    resp = client.get("/locations/attic/labels.pdf")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert 'filename="attic-labels.pdf"' in resp.headers["content-disposition"]
    assert resp.content.startswith(b"%PDF-1.4")
    assert b"Attic" in resp.content
    assert b"Roaster Parts Bin" in resp.content
    assert b"spare fans" in resp.content
    assert b"https://magpie.example.com/scan/local/l/roaster-parts-bin" in resp.content


def test_single_location_label_and_qr_do_not_expand_to_subtree(tmp_path) -> None:
    client = _app(tmp_path)
    client.post(
        "/locations",
        json={"type": "room", "label": "Attic", "capacity_class": "oversize"},
    )
    client.post(
        "/locations",
        json={
            "type": "box",
            "label": "Roaster Parts Bin",
            "parent_id": "attic",
            "capacity_class": "large",
        },
    )
    client.post(
        "/locations/roaster-parts-bin/items",
        json={"name": "spare fans", "quantity": 2},
    )

    qr = client.get("/locations/attic/qr.svg")
    label = client.get("/locations/attic/label.pdf")

    assert qr.status_code == 200
    assert qr.headers["content-type"].startswith("image/svg+xml")
    assert qr.headers["cache-control"] == "private, no-store"
    assert qr.text.lstrip().startswith("<svg")
    assert label.status_code == 200
    assert label.headers["content-type"] == "application/pdf"
    assert 'filename="Attic-label.pdf"' in label.headers["content-disposition"]
    assert label.content.startswith(b"%PDF-1.4")
    assert b"Attic" in label.content
    assert b"spare fans" in label.content
    assert b"Roaster Parts Bin" not in label.content
    assert b"https://magpie.example.com/scan/local/l/attic" in label.content


def test_labels_use_public_origin_instead_of_printer_origin(tmp_path) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "hosted", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout),
                database_path=str(tmp_path / "inventory.db"),
                public_origin="https://customer.magpie.example",
                zebra=ZebraConfig(base_url="https://printer-console.invalid"),
            )
        )
    )
    location_id = client.post(
        "/locations", json={"type": "bin", "label": "Cable adapters"}
    ).json()["id"]
    item_id = client.post(
        f"/locations/{location_id}/items", json={"name": "USB-C adapter"}
    ).json()["item_id"]

    location_label = client.get(f"/locations/{location_id}/label.pdf")
    item_label = client.get(f"/items/{item_id}/label.pdf")

    assert (
        b"https://customer.magpie.example/scan/local/l/cable-adapters"
        in location_label.content
    )
    assert (
        f"https://customer.magpie.example/scan/local/i/{item_id}".encode()
        in item_label.content
    )
    assert b"printer-console.invalid" not in location_label.content
    assert b"printer-console.invalid" not in item_label.content


def test_location_label_batch_pdf_uses_only_explicit_locations(tmp_path) -> None:
    client = _app(tmp_path)
    home_id = client.post(
        "/locations", json={"type": "home", "label": "My Home"}
    ).json()["id"]
    garage_id = client.post(
        "/locations",
        json={"type": "room", "label": "Garage", "parent_id": home_id},
    ).json()["id"]
    attic_id = client.post(
        "/locations",
        json={"type": "room", "label": "Attic", "parent_id": home_id},
    ).json()["id"]
    client.post(
        "/locations",
        json={"type": "box", "label": "Hidden Box", "parent_id": garage_id},
    )

    response = client.get(
        "/locations/labels.pdf"
        f"?location_ids={home_id},{garage_id},{attic_id},{garage_id}"
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="magpie-location-labels.pdf"'
    )
    assert response.headers["x-magpie-label-count"] == "3"
    assert response.content.startswith(b"%PDF-1.4")
    assert b"My Home" in response.content
    assert b"Garage" in response.content
    assert b"Attic" in response.content
    assert b"Hidden Box" not in response.content


def test_location_label_batch_pdf_validates_entire_selection(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "room", "label": "Attic"})

    empty = client.get("/locations/labels.pdf")
    missing = client.get(
        "/locations/labels.pdf?location_ids=attic,missing-location"
    )
    too_many = client.get(
        "/locations/labels.pdf?location_ids="
        + ",".join(f"location-{index}" for index in range(101))
    )

    assert empty.status_code == 400
    assert empty.json()["detail"] == "select at least one location"
    assert missing.status_code == 404
    assert missing.json()["detail"]["location_ids"] == ["missing-location"]
    assert too_many.status_code == 400
    assert (
        too_many.json()["detail"]
        == "label batches are limited to 100 locations"
    )


def test_scan_location_page_is_human_readable(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "room", "label": "Attic", "capacity_class": "oversize"})
    client.post(
        "/locations",
        json={
            "type": "box",
            "label": "Roaster Parts Bin",
            "parent_id": "attic",
            "capacity_class": "large",
            "purpose_tags": ["roaster", "parts"],
            "placement_notes": "Spare roaster assemblies and hardware.",
        },
    )
    client.post("/locations/roaster-parts-bin/items", json={"name": "spare fans", "quantity": 2})
    client.post("/locations/roaster-parts-bin/items", json={"name": "spare burner"})

    resp = client.get("/l/roaster-parts-bin")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert '<link rel="stylesheet" href="/theme.css">' in resp.text
    assert '<script src="/product-shell.js" defer></script>' in resp.text
    assert '<body class="product-app">' in resp.text
    assert 'class="product-nav" data-product-nav' in resp.text
    assert '<span class="product-context">Scanned location</span>' in resp.text
    assert '<h1 class="product-object-title">Roaster Parts Bin</h1>' in resp.text
    assert "Attic &gt; Roaster Parts Bin" in resp.text
    assert "spare fans" in resp.text
    assert '/scan/local/i/' in resp.text
    assert "2 each" in resp.text
    assert "Quick add here" in resp.text
    assert "Put away with Magpie" in resp.text
    assert "Manage place" in resp.text
    assert (
        'href="/?location=roaster-parts-bin&amp;prompt=Put%20away%20&amp;action=put-away"'
        in resp.text
    )
    assert 'href="/storage?location=roaster-parts-bin"' in resp.text
    assert '/mobile?location=roaster-parts-bin' not in resp.text
    assert 'id="quickPut"' in resp.text
    assert 'id="quickPhoto"' in resp.text
    assert 'id="quickEdit"' in resp.text
    assert "function showQuickEdit" in resp.text
    assert "/inventory/${lastSavedItem.item_id}" in resp.text
    assert "/locations/roaster-parts-bin/quick-put" in resp.text
    assert "Saving to this spot" in resp.text
    assert "magpie.scanQuickPut.queue.v1" in resp.text
    assert "magpie.scanQuickPut.queue.v2.${workspaceId}" in resp.text
    assert 'const workspaceId = "local"' in resp.text
    assert 'const locationId = "roaster-parts-bin"' in resp.text
    assert "queuePhotoBytesLimit = 18 * 1024 * 1024" in resp.text
    assert "entry.workspace_id === workspaceId" in resp.text
    assert "function retryQueuedQuickPuts" in resp.text
    assert "function quickPhotoPayload" in resp.text
    assert "function uploadQuickPhotoPayload" in resp.text
    assert "async function uploadQueuedPhotos(entry, persist = null)" in resp.text
    assert "entry.photo_payloads.shift()" in resp.text
    assert "if (persist) persist()" in resp.text
    assert "client_request_id:entry.client_request_id" in resp.text
    assert "failure.status = response.status" in resp.text
    assert "!error.status && (text || photoIds.length || photoPayloads.length)" in resp.text
    assert "error.status < 500" in resp.text
    assert '<script src="/photo-utils.js"></script>' in resp.text
    assert "window.magpiePhotos.prepare(file)" in resp.text
    assert "photo_payloads" in resp.text
    assert 'navigator.serviceWorker.register("/service-worker.js")' in resp.text
    assert 'type: "CACHE_SCAN_PAGE", path: window.location.pathname' in resp.text
    assert 'window.addEventListener("online", retryQueuedQuickPuts)' in resp.text
    assert "Entry queued for retry" in resp.text
    assert "interaction #" in resp.text
    assert 'id="light" type="button"' not in resp.text
    assert 'href="/locations/roaster-parts-bin/label.pdf">Download label</a>' in resp.text
    assert "action=put-away" in resp.text
    assert 'fetch("/locations/roaster-parts-bin/light", { method: "POST" })' in resp.text
    assert "/locations/roaster-parts-bin" in resp.text
    assert "Recent history" in resp.text
    assert "Stored" in resp.text
    assert "Location created" in resp.text
    assert 'class="product-bottom-nav" data-product-nav' in resp.text
    assert "prefers-color-scheme: dark" not in resp.text


def test_scan_item_page_is_human_readable(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "room", "label": "Attic", "capacity_class": "oversize"})
    client.post(
        "/locations",
        json={
            "type": "box",
            "label": "Roaster Parts Bin",
            "parent_id": "attic",
            "capacity_class": "large",
        },
    )
    created = client.post(
        "/locations/roaster-parts-bin/items",
        json={"name": "spare fans", "quantity": 2, "unit": "each"},
    ).json()

    resp = client.get(f"/i/{created['item_id']}")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert '<link rel="stylesheet" href="/theme.css">' in resp.text
    assert '<script src="/product-shell.js" defer></script>' in resp.text
    assert '<body class="product-app">' in resp.text
    assert 'class="product-nav" data-product-nav' in resp.text
    assert '<span class="product-context">Scanned item</span>' in resp.text
    assert '<h1 class="product-object-title">spare fans</h1>' in resp.text
    assert "Attic &gt; Roaster Parts Bin" in resp.text
    assert '/scan/local/l/roaster-parts-bin' in resp.text
    assert "2 each" in resp.text
    assert f"/items/{created['item_id']}/label.pdf" in resp.text
    assert "Download label" in resp.text
    assert "Find with Magpie" in resp.text
    assert "Manage item" in resp.text
    assert f'href="/storage?item={created["item_id"]}"' in resp.text
    assert (
        'href="/?prompt=Find%20spare%20fans&amp;action=find&amp;auto=find"'
        in resp.text
    )
    assert '/?q=spare%20fans' not in resp.text
    assert "Recent history" in resp.text
    assert "Stored" in resp.text
    assert "roaster-parts-bin" in resp.text
    assert 'class="product-bottom-nav" data-product-nav' in resp.text
    assert "prefers-color-scheme: dark" not in resp.text


def test_archived_item_label_and_scan_remain_manageable(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "box", "label": "Retired parts"})
    created = client.post(
        "/locations/retired-parts/items",
        json={"name": "legacy relay", "quantity": 1, "unit": "each"},
    ).json()
    item_id = created["item_id"]

    assert client.post(f"/inventory/{item_id}/archive").status_code == 200

    scanned = client.get(f"/i/{item_id}")
    assert scanned.status_code == 200
    assert "Archived item" in scanned.text
    assert "<span>Archived</span>" in scanned.text
    assert f'href="/storage?item={item_id}"' in scanned.text
    assert "Manage item" in scanned.text
    assert "Find with Magpie" not in scanned.text
    assert client.get(f"/items/{item_id}/label.pdf").status_code == 200

    history = client.get(f"/items/{item_id}/history").json()
    assert history["item_id"] == item_id
    assert history["item"]["active"] is False

    catalog = client.get(
        "/inventory/catalog",
        params={"archived": "true", "item_id": item_id},
    ).json()
    assert catalog["item_id"] == item_id
    assert [group["item_id"] for group in catalog["groups"]] == [item_id]


def test_item_history_endpoint_lists_recent_events(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "box", "label": "Roaster Parts Bin"})
    client.post("/locations", json={"type": "box", "label": "Bench Box"})
    created = client.post(
        "/locations/roaster-parts-bin/items",
        json={"name": "spare fans", "quantity": 2, "unit": "each"},
    ).json()
    client.post(
        "/inventory/move",
        json={
            "item_id": created["item_id"],
            "from_bin": "roaster-parts-bin",
            "to_bin": "bench-box",
        },
    )

    resp = client.get(f"/items/{created['item_id']}/history")

    assert resp.status_code == 200
    body = resp.json()
    assert body["item"]["name"] == "spare fans"
    assert [event["event_type"] for event in body["events"][:2]] == [
        "inventory_move",
        "inventory_put",
    ]
    assert body["events"][0]["bin_id"] == "bench-box"
    assert body["events"][0]["note"] == "from roaster-parts-bin"


def test_item_label_pdf_includes_item_scan_url(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "box", "label": "Roaster Parts Bin"})
    created = client.post(
        "/locations/roaster-parts-bin/items",
        json={"name": "spare fans", "quantity": 2, "unit": "each"},
    ).json()

    resp = client.get(f"/items/{created['item_id']}/label.pdf")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert 'filename="spare-fans-label.pdf"' in resp.headers["content-disposition"]
    assert resp.content.startswith(b"%PDF-1.4")
    assert b"spare fans" in resp.content
    assert b"Roaster Parts Bin 2 each" in resp.content
    assert (
        f"https://magpie.example.com/scan/local/i/{created['item_id']}".encode()
        in resp.content
    )


def test_item_label_batch_pdf_deduplicates_items_and_paginates(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "room", "label": "Workshop"})
    client.post(
        "/locations",
        json={"type": "box", "label": "Label Box", "parent_id": "workshop"},
    )
    item_ids = []
    for index in range(9):
        created = client.post(
            "/locations/label-box/items",
            json={"name": f"labeled item {index + 1}"},
        ).json()
        item_ids.append(created["item_id"])

    requested_ids = ",".join(str(item_id) for item_id in [item_ids[0], *item_ids])
    resp = client.get(f"/items/labels.pdf?item_ids={requested_ids}")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert (
        resp.headers["content-disposition"]
        == 'attachment; filename="magpie-item-labels.pdf"'
    )
    assert resp.headers["x-magpie-label-count"] == "9"
    assert resp.content.startswith(b"%PDF-1.4")
    assert b"/Count 2" in resp.content
    assert b"Workshop > Label Box 1 each" in resp.content
    for index, item_id in enumerate(item_ids):
        assert f"labeled item {index + 1}".encode() in resp.content
        assert (
            f"https://magpie.example.com/scan/local/i/{item_id}".encode()
            in resp.content
        )


def test_item_label_batch_pdf_validates_entire_selection(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "box", "label": "Label Box"})
    created = client.post(
        "/locations/label-box/items",
        json={"name": "known item"},
    ).json()

    missing = client.get(
        f"/items/labels.pdf?item_ids={created['item_id']},999999"
    )
    invalid = client.get("/items/labels.pdf?item_ids=one")
    too_many = client.get(
        "/items/labels.pdf?item_ids=" + ",".join(str(index) for index in range(1, 102))
    )

    assert missing.status_code == 404
    assert missing.json()["detail"]["item_ids"] == [999999]
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "item IDs must be integers"
    assert too_many.status_code == 400
    assert too_many.json()["detail"] == "label batches are limited to 100 items"


def test_scan_quick_put_saves_item_and_attaches_photo(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "box", "label": "Bench Box", "capacity_class": "medium"})
    photo = client.post(
        "/photos",
        json={
            "filename": "part.jpg",
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(b"image").decode("ascii"),
        },
    ).json()["photo"]

    response = client.post(
        "/locations/bench-box/quick-put",
        json={"text": "blue pry tool", "photo_ids": [photo["id"]]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "put_away"
    assert body["message"] == "Stored Blue Pry Tool in Bench Box."
    assert body["interaction_id"] > 0
    assert body["destination"]["id"] == "bench-box"
    assert body["item"]["name"] == "Blue Pry Tool"
    assert body["item"]["photo_ids"] == [photo["id"]]
    items = client.get("/locations/bench-box").json()["items"]
    assert items[0]["name"] == "Blue Pry Tool"
    assert f"/photos/{photo['id']}" in items[0]["photos"]
    interaction = client.get("/assistant/interactions?limit=1").json()["interactions"][0]
    assert interaction["id"] == body["interaction_id"]
    assert interaction["route"] == "/locations/bench-box/quick-put"
    assert interaction["command"] == "blue pry tool"
    assert interaction["photo_ids"] == [photo["id"]]
    assert interaction["response"]["interaction_id"] == body["interaction_id"]
    assert interaction["response"]["item"]["name"] == "Blue Pry Tool"
    assert interaction["response"]["message"] == body["message"]


def test_scan_quick_put_replay_is_idempotent(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "box", "label": "Bench Box", "capacity_class": "medium"})
    payload = {"text": "blue pry tool", "client_request_id": "scan-request-123"}

    first = client.post("/locations/bench-box/quick-put", json=payload)
    replay = client.post("/locations/bench-box/quick-put", json=payload)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == {**first.json(), "idempotent_replay": True}
    assert first.json()["client_request_id"] == payload["client_request_id"]
    items = client.get("/locations/bench-box").json()["items"]
    assert items[0]["quantity"] == 1
    interactions = client.get("/assistant/interactions?limit=10").json()["interactions"]
    matching = [row for row in interactions if row["route"] == "/locations/bench-box/quick-put"]
    assert len(matching) == 1

    invalid = client.post(
        "/locations/bench-box/quick-put",
        json={"text": "another tool", "client_request_id": "not allowed / value"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "client_request_id is invalid"


def test_quick_put_and_item_update_reject_organizer_only_destination(tmp_path) -> None:
    client = _app(tmp_path)
    client.post(
        "/locations",
        json={"type": "room", "label": "Attic"},
    )
    created = client.post(
        "/inventory",
        json={"name": "M4 T-nuts", "bin_id": "B001", "quantity": 1},
    ).json()

    quick_put = client.post(
        "/locations/attic/quick-put",
        json={"text": "holiday lights"},
    )
    update = client.post(
        f"/inventory/{created['item_id']}",
        json={
            "name": "M4 T-nuts",
            "bin_id": "attic",
            "quantity": 1,
            "unit": "each",
            "category": "fasteners",
            "description": "",
            "aliases": [],
        },
    )

    assert quick_put.status_code == 400
    assert "does not hold items directly" in quick_put.json()["detail"]
    assert update.status_code == 400
    assert "does not hold items directly" in update.json()["detail"]
    assert client.get("/locations/attic").json()["items"] == []


def test_scan_quick_put_item_can_be_corrected_in_arbitrary_location(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "box", "label": "Bench Box", "capacity_class": "medium"})
    created = client.post(
        "/locations/bench-box/quick-put",
        json={"text": "blue pry tool"},
    ).json()

    response = client.post(
        f"/inventory/{created['item_id']}",
        json={
            "name": "Blue nylon pry tool",
            "bin_id": "bench-box",
            "quantity": 2,
            "unit": "each",
            "category": "tools",
            "description": "",
            "aliases": ["spudger"],
        },
    )

    assert response.status_code == 200
    assert response.json()["bin_id"] == "bench-box"
    items = client.get("/locations/bench-box").json()["items"]
    assert items[0]["name"] == "Blue nylon pry tool"
    assert items[0]["quantity"] == 2
    assert items[0]["category"] == "tools"
    assert items[0]["aliases"] == ["spudger"]
    example = InventoryStore(tmp_path / "inventory.db").recent_examples(limit=1)[0]
    assert example["kind"] == "item_correction"
    assert example["command"] == "item corrected Blue Pry Tool to Blue nylon pry tool"
    payload = json.loads(example["action"])
    assert payload["before"]["name"] == "Blue Pry Tool"
    assert payload["after"]["name"] == "Blue nylon pry tool"
    assert payload["after"]["bin_id"] == "bench-box"


def test_inventory_export_json_includes_locations_items_and_rules(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "room", "label": "Attic", "capacity_class": "oversize"})
    client.post(
        "/locations",
        json={
            "type": "box",
            "label": "Roaster Parts Bin",
            "parent_id": "attic",
            "capacity_class": "large",
            "purpose_tags": ["roaster"],
        },
    )
    client.post(
        "/locations/roaster-parts-bin/items",
        json={"name": "spare fans", "quantity": 2, "unit": "each"},
    )
    client.post(
        "/assistant/confirm",
        json={
            "command": "I always keep roaster parts in the attic",
            "plan": [
                {
                    "tool": "learn_rule",
                    "arguments": {
                        "rule_text": "Roaster parts go in the attic.",
                        "trigger_terms": ["roaster", "parts"],
                        "target_location_id": "attic",
                    },
                },
                {
                    "tool": "learn_size_rule",
                    "arguments": {
                        "rule_text": "Roaster assemblies are large.",
                        "trigger_terms": ["roaster", "assembly"],
                        "size_class": "large",
                    },
                },
            ],
        },
    )

    response = client.get("/export/inventory.json")

    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "magpie.inventory.v1"
    assert body["build"]["version"]
    assert any(loc["id"] == "roaster-parts-bin" for loc in body["locations"])
    exported_item = next(item for item in body["inventory"] if item["name"] == "spare fans")
    assert exported_item["bin_id"] == "roaster-parts-bin"
    assert exported_item["quantity"] == 2
    assert body["placement_rules"][0]["target_location_id"] == "attic"
    assert body["size_rules"][0]["size_class"] == "large"


def test_inventory_json_import_restores_exported_snapshot(tmp_path) -> None:
    source_path = tmp_path / "source"
    target_path = tmp_path / "target"
    source_path.mkdir()
    target_path.mkdir()
    source = _app(source_path)
    source.post("/locations", json={"type": "room", "label": "Attic", "capacity_class": "oversize"})
    source.post(
        "/locations",
        json={
            "type": "box",
            "label": "Roaster Parts Bin",
            "parent_id": "attic",
            "capacity_class": "large",
            "purpose_tags": ["roaster"],
        },
    )
    source.post(
        "/locations/roaster-parts-bin/items",
        json={"name": "spare fans", "quantity": 2, "unit": "each"},
    )
    source.post(
        "/assistant/confirm",
        json={
            "command": "I always keep roaster parts in the attic",
            "plan": [
                {
                    "tool": "learn_rule",
                    "arguments": {
                        "rule_text": "Roaster parts go in the attic.",
                        "trigger_terms": ["roaster", "parts"],
                        "target_location_id": "attic",
                    },
                },
                {
                    "tool": "learn_size_rule",
                    "arguments": {
                        "rule_text": "Roaster assemblies are large.",
                        "trigger_terms": ["roaster", "assembly"],
                        "size_class": "large",
                    },
                },
            ],
        },
    )
    snapshot = source.get("/export/inventory.json").json()
    target = _app(target_path)

    dry_run = target.post("/import/inventory.json", json={"snapshot": snapshot, "dry_run": True})
    imported = target.post("/import/inventory.json", json={"snapshot": snapshot})

    assert dry_run.status_code == 200
    assert dry_run.json()["locations"] == len(snapshot["locations"])
    assert dry_run.json()["inventory"] == 1
    assert dry_run.json()["placement_rules"] == 1
    assert dry_run.json()["size_rules"] == 1
    assert imported.status_code == 200
    assert imported.json()["imported"] == 1
    restored = target.get("/locations/roaster-parts-bin").json()
    assert restored["location"]["parent_id"] == "attic"
    assert restored["items"][0]["name"] == "spare fans"
    assert restored["items"][0]["quantity"] == 2
    exported = target.get("/export/inventory.json").json()
    assert exported["placement_rules"][0]["target_location_id"] == "attic"
    assert exported["size_rules"][0]["size_class"] == "large"


def test_inventory_json_import_orders_parents_and_preserves_location_photos(
    tmp_path,
) -> None:
    source_path = tmp_path / "source-unordered"
    target_path = tmp_path / "target-unordered"
    source_path.mkdir()
    target_path.mkdir()
    source = _app(source_path)
    source.post(
        "/locations",
        json={"id": "attic", "type": "room", "label": "Attic"},
    )
    source.post(
        "/locations",
        json={
            "id": "parts-bin",
            "type": "box",
            "label": "Parts Bin",
            "parent_id": "attic",
        },
    )
    snapshot = source.get("/export/inventory.json").json()
    snapshot["photos"] = [
        {
            "id": 101,
            "filename": "attic.jpg",
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(b"attic-photo").decode("ascii"),
        },
        {
            "id": 102,
            "filename": "parts.jpg",
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(b"parts-photo").decode("ascii"),
        },
    ]
    for row in snapshot["locations"]:
        if row["id"] == "attic":
            row["photo_ids"] = [101]
        elif row["id"] == "parts-bin":
            row["photo_ids"] = [102]
    snapshot["locations"].sort(
        key=lambda row: (
            0
            if row["id"] == "parts-bin"
            else 2 if row["id"] == "attic" else 1
        )
    )

    target = _app(target_path)
    imported = target.post("/import/inventory.json", json={"snapshot": snapshot})

    assert imported.status_code == 200
    assert (
        target.get("/locations/parts-bin").json()["location"]["parent_id"]
        == "attic"
    )
    restored = target.get("/export/inventory.json").json()
    photos_by_id = {row["id"]: row["filename"] for row in restored["photos"]}
    locations_by_id = {row["id"]: row for row in restored["locations"]}
    assert [
        photos_by_id[photo_id]
        for photo_id in locations_by_id["attic"]["photo_ids"]
    ] == ["attic.jpg"]
    assert [
        photos_by_id[photo_id]
        for photo_id in locations_by_id["parts-bin"]["photo_ids"]
    ] == ["parts.jpg"]


def test_inventory_json_round_trip_preserves_item_state_and_inactive_rules(
    tmp_path,
) -> None:
    source_path = tmp_path / "source-state"
    target_path = tmp_path / "target-state"
    source_path.mkdir()
    target_path.mkdir()
    source = _app(source_path)
    created = source.post(
        "/locations/B001/items",
        json={
            "name": "M4 shoulder screw",
            "description": "Precision fixture spare",
            "category": "fasteners",
            "aliases": ["fixture screw"],
            "quantity": 4,
        },
    ).json()
    item_id = created["item_id"]
    source_store = InventoryStore(source_path / "inventory.db")
    source_store.put_inventory(
        item_id=item_id,
        bin_id="B002",
        quantity=2,
        unit="each",
        needs_review=True,
        review_reason="Verify the short shoulder length.",
        normalize_bin=False,
    )
    source_store.restore_item_size_metadata(
        item_id=item_id,
        size_class="small",
        source="calibrated_photo",
        confidence=0.93,
        dimensions={
            "width_mm": 8.0,
            "depth_mm": 24.0,
            "height_mm": 8.0,
        },
    )
    source_store.restore_item_location_metadata(
        item_id=item_id,
        home_location_id="B001",
        last_seen_location_id="B002",
        active=False,
        archived_at="2026-07-28T14:30:00+00:00",
    )
    placement_rule_id = source_store.learn_rule(
        rule_text="Fixture screws stay in the main cabinet.",
        trigger_terms=["fixture", "screws"],
        target_location_id="B001",
        source_command="backup fixture",
        confidence=0.88,
    )
    size_rule_id = source_store.learn_size_rule(
        rule_text="Shoulder screws use small storage.",
        trigger_terms=["shoulder", "screws"],
        size_class="small",
        source_command="backup fixture",
        confidence=0.91,
    )
    assert source_store.set_rule_active(
        rule_id=placement_rule_id, rule_type="placement", active=False
    )
    assert source_store.set_rule_active(
        rule_id=size_rule_id, rule_type="size", active=False
    )

    snapshot = source.get("/export/inventory.json").json()
    exported_rows = [
        row for row in snapshot["inventory"] if row["name"] == "M4 shoulder screw"
    ]
    assert len(exported_rows) == 2
    assert all(row["active"] is False for row in exported_rows)
    assert snapshot["placement_rules"][0]["active"] is False
    assert snapshot["size_rules"][0]["active"] is False

    target = _app(target_path)
    preview = target.post(
        "/import/inventory.json",
        json={"snapshot": snapshot, "dry_run": True},
    )
    restored = target.post("/import/inventory.json", json={"snapshot": snapshot})

    assert preview.status_code == 200
    assert preview.json()["inventory"] == 2
    assert preview.json()["placement_rules"] == 1
    assert preview.json()["size_rules"] == 1
    assert restored.status_code == 200
    target_store = InventoryStore(target_path / "inventory.db")
    restored_rows = target_store.list_inventory(include_archived=True)
    assert len(restored_rows) == 2
    assert {row.bin_id for row in restored_rows} == {"B001", "B002"}
    for row in restored_rows:
        assert row.active is False
        assert row.archived_at == "2026-07-28T14:30:00+00:00"
        assert row.size_class == "small"
        assert row.size_source == "calibrated_photo"
        assert row.size_confidence == 0.93
        assert row.dimensions == {
            "width_mm": 8.0,
            "depth_mm": 24.0,
            "height_mm": 8.0,
        }
        assert row.home_location_id == "B001"
        assert row.last_seen_location_id == "B002"
    review_row = next(row for row in restored_rows if row.bin_id == "B002")
    assert review_row.needs_review is True
    assert review_row.review_reason == "Verify the short shoulder length."
    assert target_store.rules(active_only=False)[0].active is False
    assert target_store.size_rules(active_only=False)[0].active is False


def test_inventory_json_round_trip_preserves_item_and_location_photos(tmp_path) -> None:
    source_path = tmp_path / "source-photos"
    target_path = tmp_path / "target-photos"
    source_path.mkdir()
    target_path.mkdir()
    source = _app(source_path)
    source.post("/locations", json={"type": "box", "label": "Camera Parts"})
    item_photo = source.post(
        "/photos",
        json={
            "filename": "lens.jpg",
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(b"item image bytes").decode("ascii"),
        },
    ).json()["photo"]
    location_photo = source.post(
        "/photos",
        json={
            "filename": "box.png",
            "content_type": "image/png",
            "data_base64": base64.b64encode(b"location image bytes").decode("ascii"),
        },
    ).json()["photo"]
    source.post(
        "/locations/camera-parts/quick-put",
        json={"text": "spare lens", "photo_ids": [item_photo["id"]]},
    )
    attached = source.post(
        "/photos/attach-retry",
        json={
            "photo_ids": [location_photo["id"]],
            "location_id": "camera-parts",
        },
    )
    assert attached.status_code == 200

    snapshot = source.get("/export/inventory.json").json()
    assert len(snapshot["photos"]) == 2
    assert snapshot["inventory"][0]["photo_ids"] == [item_photo["id"]]
    exported_location = next(
        row for row in snapshot["locations"] if row["id"] == "camera-parts"
    )
    assert exported_location["photo_ids"] == [location_photo["id"]]

    target = _app(target_path)
    target.post(
        "/photos",
        json={
            "filename": "existing.jpg",
            "content_type": "image/jpeg",
            "data_base64": base64.b64encode(b"existing").decode("ascii"),
        },
    )
    preview = target.post(
        "/import/inventory.json", json={"snapshot": snapshot, "dry_run": True}
    )
    restored = target.post("/import/inventory.json", json={"snapshot": snapshot})

    assert preview.status_code == 200
    assert preview.json()["photos"] == 2
    assert restored.status_code == 200
    assert restored.json()["photos"] == 2
    location = target.get("/locations/camera-parts").json()
    item_photo_url = location["items"][0]["photos"][0]
    location_photo_url = location["location"]["photos"][0]["url"]
    assert target.get(item_photo_url).content == b"item image bytes"
    assert target.get(location_photo_url).content == b"location image bytes"


def test_inventory_json_import_rejects_dangling_photo_reference_before_writing(
    tmp_path,
) -> None:
    client = _app(tmp_path)
    before = {
        row["id"]
        for row in client.get("/locations?include_archived=true").json()["locations"]
    }
    snapshot = {
        "schema": "magpie.inventory.v1",
        "locations": [
            {"id": "camera-parts", "type": "box", "label": "Camera Parts"}
        ],
        "inventory": [
            {
                "name": "spare lens",
                "bin_id": "camera-parts",
                "photo_ids": [42],
            }
        ],
        "photos": [],
        "placement_rules": [],
        "size_rules": [],
    }

    response = client.post("/import/inventory.json", json={"snapshot": snapshot})

    assert response.status_code == 400
    assert "unknown photo 42" in response.text
    after = {
        row["id"]
        for row in client.get("/locations?include_archived=true").json()["locations"]
    }
    assert after == before


def test_inventory_json_round_trip_preserves_archived_graph_items_and_dimensions(
    tmp_path,
) -> None:
    source_path = tmp_path / "source-archived"
    target_path = tmp_path / "target-archived"
    source_path.mkdir()
    target_path.mkdir()
    source = _app(source_path)
    source.post("/locations", json={"type": "room", "label": "Attic"})
    source.post(
        "/locations",
        json={
            "type": "box",
            "label": "Holiday Box",
            "parent_id": "attic",
            "internal_dimensions": {
                "width_mm": 420,
                "depth_mm": 310,
                "height_mm": 180,
            },
        },
    )
    source.post(
        "/locations/holiday-box/items",
        json={"name": "tree lights", "quantity": 3, "unit": "strands"},
    )
    assert source.post("/locations/attic/archive").status_code == 200

    snapshot = source.get("/export/inventory.json").json()
    exported_locations = {row["id"]: row for row in snapshot["locations"]}
    assert exported_locations["attic"]["active"] is False
    assert exported_locations["holiday-box"]["active"] is False
    assert exported_locations["holiday-box"]["internal_dimensions"] == {
        "width_mm": 420.0,
        "depth_mm": 310.0,
        "height_mm": 180.0,
    }

    target = _app(target_path)
    preview = target.post(
        "/import/inventory.json", json={"snapshot": snapshot, "dry_run": True}
    )
    restored = target.post("/import/inventory.json", json={"snapshot": snapshot})

    assert preview.status_code == 200
    assert restored.status_code == 200
    archived = {
        row["id"]: row
        for row in target.get("/locations?include_archived=true").json()["locations"]
    }
    assert archived["attic"]["active"] is False
    assert archived["holiday-box"]["active"] is False
    assert target.post("/locations/attic/restore").status_code == 200
    holiday_box = target.get("/locations/holiday-box").json()
    assert holiday_box["location"]["internal_dimensions"] == {
        "width_mm": 420.0,
        "depth_mm": 310.0,
        "height_mm": 180.0,
    }
    assert holiday_box["items"][0]["name"] == "tree lights"
    assert holiday_box["items"][0]["quantity"] == 3


def test_inventory_json_import_rejects_duplicate_and_cyclic_locations_before_writing(
    tmp_path,
) -> None:
    client = _app(tmp_path)
    duplicate_snapshot = {
        "schema": "magpie.inventory.v1",
        "locations": [
            {"id": "garage", "type": "room", "label": "Garage"},
            {"id": "garage", "type": "room", "label": "Other Garage"},
        ],
        "inventory": [],
        "placement_rules": [],
        "size_rules": [],
    }
    cyclic_snapshot = {
        "schema": "magpie.inventory.v1",
        "locations": [
            {"id": "garage", "type": "room", "label": "Garage", "parent_id": "shelf"},
            {"id": "shelf", "type": "shelf", "label": "Shelf", "parent_id": "garage"},
        ],
        "inventory": [],
        "placement_rules": [],
        "size_rules": [],
    }

    duplicate = client.post(
        "/import/inventory.json", json={"snapshot": duplicate_snapshot}
    )
    cyclic = client.post("/import/inventory.json", json={"snapshot": cyclic_snapshot})

    assert duplicate.status_code == 400
    assert "duplicate ids" in duplicate.text
    assert cyclic.status_code == 400
    assert "contains a cycle" in cyclic.text
    location_ids = {
        row["id"]
        for row in client.get("/locations?include_archived=true").json()["locations"]
    }
    assert "garage" not in location_ids
    assert "shelf" not in location_ids


def test_inventory_json_import_validates_snapshot_before_writing(tmp_path) -> None:
    client = _app(tmp_path)
    snapshot = {
        "schema": "magpie.inventory.v1",
        "locations": [],
        "inventory": [{"name": "lost item", "bin_id": "missing-place", "quantity": 1}],
        "placement_rules": [],
        "size_rules": [],
    }

    response = client.post("/import/inventory.json", json={"snapshot": snapshot})

    assert response.status_code == 400
    assert "unknown location missing-place" in response.text
    assert client.get("/inventory").json()["items"] == []


def test_inventory_json_import_rejects_items_in_organizer_only_places(tmp_path) -> None:
    client = _app(tmp_path)
    snapshot = {
        "schema": "magpie.inventory.v1",
        "locations": [
            {
                "id": "attic",
                "type": "room",
                "label": "Attic",
                "accepts_items": False,
            }
        ],
        "inventory": [{"name": "Heirloom jewelry", "bin_id": "attic"}],
        "placement_rules": [],
        "size_rules": [],
    }

    response = client.post("/import/inventory.json", json={"snapshot": snapshot})

    assert response.status_code == 400
    assert "inventory[1]" in response.text
    assert "does not hold items directly" in response.text
    assert client.get("/inventory").json()["items"] == []
    assert client.get("/locations/attic").status_code == 404


def test_inventory_pdf_export_is_printable_and_includes_archived_paths(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "room", "label": "Attic"})
    client.post(
        "/locations",
        json={"type": "box", "label": "Holiday Box", "parent_id": "attic"},
    )
    client.post(
        "/locations/holiday-box/items",
        json={"name": "tree lights", "quantity": 3, "unit": "strands"},
    )
    client.post("/locations/attic/archive")

    response = client.get("/export/inventory.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == (
        'attachment; filename="magpie-inventory.pdf"'
    )
    assert response.content.startswith(b"%PDF-1.4")
    assert b"Magpie inventory" in response.content
    assert b"tree lights" in response.content
    assert b"Attic > Holiday Box [archived]" in response.content


def test_inventory_csv_import_supports_dry_run_and_import(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "box", "label": "Bench Box", "capacity_class": "medium"})
    csv_text = (
        "name,location_id,quantity,unit,category,aliases,description\n"
        "spare fans,bench-box,2,each,parts,fan;blower,quiet spares\n"
        "M3 screws,B001,12,each,fasteners,machine screws,\n"
    )

    dry_run = client.post(
        "/import/inventory.csv",
        json={"csv_text": csv_text, "dry_run": True},
    )

    assert dry_run.status_code == 200
    assert dry_run.json()["imported"] == 0
    assert dry_run.json()["rows"][0]["location_id"] == "bench-box"
    assert client.get("/locations/bench-box").json()["items"] == []

    imported = client.post(
        "/import/inventory.csv",
        json={"csv_text": csv_text},
    )

    assert imported.status_code == 200
    assert imported.json()["imported"] == 2
    assert imported.json()["action"] == "inventory_csv_imported"
    assert imported.json()["interaction_id"] > 0
    bench_items = client.get("/locations/bench-box").json()["items"]
    assert bench_items[0]["name"] == "spare fans"
    assert bench_items[0]["quantity"] == 2
    assert "fan" in bench_items[0]["aliases"]
    assert client.get("/locations/B001").json()["items"][0]["name"] == "M3 screws"

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    assert client.get("/locations/bench-box").json()["items"] == []
    assert client.get("/locations/B001").json()["items"] == []


def test_inventory_csv_import_can_review_and_apply_a_default_location(
    tmp_path,
) -> None:
    client = _app(tmp_path)
    csv_text = "name,quantity\nUSB adapters,3\nspare cables,2\n"

    preview = client.post(
        "/import/inventory.csv",
        json={"csv_text": csv_text, "dry_run": True},
    )

    assert preview.status_code == 200
    assert preview.json()["requires_default_location"] is True
    assert preview.json()["missing_location_rows"] == [2, 3]
    assert [row["location_id"] for row in preview.json()["rows"]] == ["", ""]

    rejected = client.post(
        "/import/inventory.csv",
        json={"csv_text": csv_text},
    )
    assert rejected.status_code == 400
    assert "unknown location (missing)" in rejected.text

    imported = client.post(
        "/import/inventory.csv",
        json={"csv_text": csv_text, "default_location_id": "B001"},
    )

    assert imported.status_code == 200
    assert imported.json()["imported"] == 2
    items = client.get("/locations/B001").json()["items"]
    assert {item["name"]: item["quantity"] for item in items} == {
        "spare cables": 2,
        "USB adapters": 3,
    }


def test_inventory_csv_import_validates_all_rows_before_writing(tmp_path) -> None:
    client = _app(tmp_path)
    csv_text = (
        "name,location_id,quantity\n"
        "valid item,B001,1\n"
        "lost item,no-such-place,3\n"
    )

    response = client.post("/import/inventory.csv", json={"csv_text": csv_text})

    assert response.status_code == 400
    assert "unknown location no-such-place" in response.text
    assert client.get("/locations/B001").json()["items"] == []


def test_inventory_csv_import_rejects_organizer_only_destination(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "room", "label": "Attic"})
    csv_text = (
        "name,location_id,quantity\n"
        "valid item,B001,1\n"
        "Heirloom jewelry,attic,1\n"
    )

    response = client.post("/import/inventory.csv", json={"csv_text": csv_text})

    assert response.status_code == 400
    assert "row 3" in response.text
    assert "does not hold items directly" in response.text
    assert client.get("/locations/B001").json()["items"] == []
    assert client.get("/locations/attic").json()["items"] == []


def test_inventory_csv_import_resolves_unique_labels_and_qualified_paths(tmp_path) -> None:
    client = _app(tmp_path)
    garage_id = client.post(
        "/locations", json={"type": "room", "label": "Garage"}
    ).json()["id"]
    attic_id = client.post(
        "/locations", json={"type": "room", "label": "Attic"}
    ).json()["id"]
    garage_drawer = client.post(
        "/locations",
        json={"type": "drawer", "label": "Drawer 1", "parent_id": garage_id},
    ).json()["id"]
    client.post(
        "/locations",
        json={"type": "drawer", "label": "Drawer 1", "parent_id": attic_id},
    )
    client.post(
        "/locations",
        json={"type": "box", "label": "Battery Box", "parent_id": garage_id},
    )
    csv_text = (
        "name,location,quantity\n"
        "AA batteries,Battery Box,8\n"
        "work gloves,Garage > Drawer 1,1\n"
    )

    preview = client.post(
        "/import/inventory.csv", json={"csv_text": csv_text, "dry_run": True}
    )

    assert preview.status_code == 200
    assert [row["location_id"] for row in preview.json()["rows"]] == [
        "battery-box",
        garage_drawer,
    ]


def test_inventory_csv_import_falls_back_to_portable_location_path(tmp_path) -> None:
    client = _app(tmp_path)
    garage_id = client.post(
        "/locations", json={"type": "room", "label": "Garage"}
    ).json()["id"]
    drawer_id = client.post(
        "/locations",
        json={"type": "drawer", "label": "Drawer 1", "parent_id": garage_id},
    ).json()["id"]
    csv_text = (
        "name,location_id,location_path,quantity\n"
        "M4 T-nuts,source-workspace-id,Garage > Drawer 1,12\n"
    )

    preview = client.post(
        "/import/inventory.csv", json={"csv_text": csv_text, "dry_run": True}
    )

    assert preview.status_code == 200
    assert preview.json()["rows"] == [
        {
            "name": "M4 T-nuts",
            "location_id": drawer_id,
            "location_path": "Garage > Drawer 1",
            "quantity": 12.0,
            "unit": "each",
            "category": "",
            "description": "",
            "aliases": [],
        }
    ]


def test_inventory_csv_export_round_trips_by_path_when_ids_change(tmp_path) -> None:
    source_path = tmp_path / "source"
    source_path.mkdir()
    source = _app(source_path)
    source_root = source.post(
        "/locations", json={"type": "room", "label": "Garage"}
    ).json()["id"]
    source_drawer = source.post(
        "/locations",
        json={
            "id": "source-drawer",
            "type": "drawer",
            "label": "Drawer 1",
            "parent_id": source_root,
        },
    ).json()["id"]
    source.post(
        "/inventory",
        json={"name": "M4 T-nuts", "bin_id": source_drawer, "quantity": 12},
    )
    exported = source.get("/export/inventory.csv").text

    destination_path = tmp_path / "destination"
    destination_path.mkdir()
    destination = _app(destination_path)

    imported = destination.post(
        "/import/inventory.csv",
        json={"csv_text": exported, "create_missing_locations": True},
    )

    assert imported.status_code == 200
    destination_drawer = imported.json()["rows"][0]["location_id"]
    assert destination_drawer == "garage-drawer-1"
    stored = destination.get(f"/locations/{destination_drawer}").json()["items"]
    assert stored[0]["name"] == "M4 T-nuts"
    assert destination.get("/locations/garage").json()["location"]["type"] == "room"
    assert (
        destination.get(f"/locations/{destination_drawer}").json()["location"]["type"]
        == "drawer"
    )


def test_inventory_csv_import_previews_and_atomically_creates_missing_paths(
    tmp_path,
) -> None:
    client = _app(tmp_path)
    csv_text = (
        "name,location_path,location_path_types,quantity\n"
        "holiday lights,My Home > Attic > Holiday cabinet > Lights bin,"
        "home > room > cabinet > bin,3\n"
    )

    preview = client.post(
        "/import/inventory.csv",
        json={"csv_text": csv_text, "dry_run": True},
    )

    assert preview.status_code == 200
    body = preview.json()
    assert body["requires_location_creation"] is True
    assert [location["path"] for location in body["missing_locations"]] == [
        "My Home",
        "My Home > Attic",
        "My Home > Attic > Holiday cabinet",
        "My Home > Attic > Holiday cabinet > Lights bin",
    ]
    assert [location["type"] for location in body["missing_locations"]] == [
        "home",
        "room",
        "cabinet",
        "bin",
    ]

    unreviewed = client.post(
        "/import/inventory.csv",
        json={"csv_text": csv_text},
    )
    assert unreviewed.status_code == 409
    assert "review and approve" in unreviewed.text
    assert client.get("/locations/my-home").status_code == 404

    imported = client.post(
        "/import/inventory.csv",
        json={"csv_text": csv_text, "create_missing_locations": True},
    )

    assert imported.status_code == 200
    result = imported.json()
    assert result["imported"] == 1
    assert len(result["created_locations"]) == 4
    destination_id = result["rows"][0]["location_id"]
    destination = client.get(f"/locations/{destination_id}").json()
    assert destination["location"]["type"] == "bin"
    assert destination["items"][0]["name"] == "holiday lights"

    undone = client.post("/assistant/undo").json()
    assert undone["ok"] is True
    assert client.get("/locations/my-home").status_code == 404


def test_inventory_csv_missing_path_defaults_to_organizers_and_bin(tmp_path) -> None:
    client = _app(tmp_path)
    preview = client.post(
        "/import/inventory.csv",
        json={
            "csv_text": "name,location_path\nUSB adapters,Workshop > Electronics\n",
            "dry_run": True,
        },
    )

    assert preview.status_code == 200
    assert [
        (location["type"], location["accepts_items"])
        for location in preview.json()["missing_locations"]
    ] == [("area", False), ("bin", True)]


def test_inventory_csv_import_rejects_ambiguous_location_labels(tmp_path) -> None:
    client = _app(tmp_path)
    for root_name in ("Garage", "Attic"):
        root_id = client.post(
            "/locations", json={"type": "room", "label": root_name}
        ).json()["id"]
        client.post(
            "/locations",
            json={"type": "drawer", "label": "Drawer 1", "parent_id": root_id},
        )

    response = client.post(
        "/import/inventory.csv",
        json={"csv_text": "name,location\nspare cable,Drawer 1\n", "dry_run": True},
    )

    assert response.status_code == 400
    assert "ambiguous location Drawer 1" in response.text
    assert "Garage > Drawer 1" in response.text
    assert "Attic > Drawer 1" in response.text


def test_inventory_csv_import_undo_restores_previous_item_state(tmp_path) -> None:
    client = _app(tmp_path)
    created = client.post(
        "/inventory",
        json={
            "name": "M3 screws",
            "description": "Original zinc screws",
            "category": "hardware",
            "aliases": ["three millimeter screws"],
            "bin_id": "B001",
            "quantity": 5,
            "unit": "each",
        },
    ).json()
    InventoryStore(tmp_path / "inventory.db").mark_needs_review(
        item_id=created["item_id"],
        bin_id="B001",
        reason="verify count",
    )

    imported = client.post(
        "/import/inventory.csv",
        json={
            "csv_text": (
                "name,location,quantity,description,category,aliases\n"
                "M3 screws,B001,20,Imported screws,fasteners,m3 bolts\n"
            )
        },
    )

    assert imported.status_code == 200
    changed = client.get("/locations/B001").json()["items"][0]
    assert changed["quantity"] == 20
    assert changed["description"] == "Imported screws"
    assert changed["category"] == "fasteners"
    assert changed["aliases"] == ["m3 bolts", "three millimeter screws"]
    assert changed["needs_review"] is False

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    restored = client.get("/locations/B001").json()["items"][0]
    assert restored["name"] == "M3 screws"
    assert restored["quantity"] == 5
    assert restored["description"] == "Original zinc screws"
    assert restored["category"] == "hardware"
    assert restored["aliases"] == ["three millimeter screws"]
    assert restored["needs_review"] is True
    assert restored["review_reason"] == "verify count"


def test_inventory_csv_import_rolls_back_rows_after_storage_failure(
    tmp_path, monkeypatch
) -> None:
    client = _app(tmp_path)
    created = client.post(
        "/inventory",
        json={
            "name": "M3 screws",
            "description": "Original screws",
            "category": "hardware",
            "aliases": ["m3 hardware"],
            "bin_id": "B001",
            "quantity": 5,
            "unit": "each",
        },
    ).json()
    store = InventoryStore(tmp_path / "inventory.db")
    store.mark_needs_review(
        item_id=created["item_id"],
        bin_id="B001",
        reason="original review",
    )
    from searchable_storage_led import store as store_module

    original_inferred_dimensions = store_module.inferred_dimensions

    def fail_second_import_write(item_name):
        if item_name == "USB cable":
            raise RuntimeError("simulated storage interruption")
        return original_inferred_dimensions(item_name)

    monkeypatch.setattr(store_module, "inferred_dimensions", fail_second_import_write)

    response = client.post(
        "/import/inventory.csv",
        json={
            "csv_text": (
                "name,location,quantity,description,category,aliases\n"
                "M3 screws,B001,20,Imported screws,fasteners,m3 bolts\n"
                "USB cable,B002,2,New cable,electronics,charging cable\n"
            )
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "inventory import could not be completed; no rows were imported"
    )
    [restored] = InventoryStore(tmp_path / "inventory.db").item_inventory(
        created["item_id"]
    )
    assert restored.quantity == 5
    assert restored.description == "Original screws"
    assert restored.category == "hardware"
    assert restored.aliases == ["m3 hardware"]
    assert restored.needs_review is True
    assert restored.review_reason == "original review"
    assert InventoryStore(tmp_path / "inventory.db").search("USB cable") == []


def test_confirm_plan_attaches_uploaded_photo_to_created_location(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "room", "label": "Attic", "capacity_class": "oversize"})
    photo = client.post(
        "/photos",
        json={
            "filename": "printer-bin.HEIC",
            "content_type": "image/heic",
            "data_base64": base64.b64encode(b"heic image").decode("ascii"),
        },
    ).json()["photo"]

    resp = client.post(
        "/assistant/confirm",
        json={
            "command": "printer supplies bin in attic",
            "photo_ids": [photo["id"]],
            "plan": [
                {
                    "tool": "create_location",
                    "arguments": {
                        "type": "bin",
                        "label": "Printer Supplies Bin",
                        "parent_id": "attic",
                    },
                },
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "Zebra label rolls",
                        "location_id": "printer-supplies-bin",
                    },
                },
            ],
        },
    )

    assert resp.status_code == 200
    body = client.get("/locations/printer-supplies-bin").json()
    assert body["location"]["photos"] == []
    assert body["contents"][0]["photos"] == [f"/photos/{photo['id']}"]


def test_print_location_label_sends_zpl(tmp_path, monkeypatch) -> None:
    client = _app(tmp_path, zebra=ZebraConfig(host="printer.local"))
    client.post("/locations", json={"type": "box", "label": "Roaster Parts Bin"})
    sent = {}

    def fake_send(zpl, cfg):
        sent["zpl"] = zpl
        sent["host"] = cfg.host

    monkeypatch.setattr("searchable_storage_led.api.send_zpl", fake_send)

    resp = client.post("/locations/roaster-parts-bin/print-label")

    assert resp.status_code == 200
    assert "ROASTER PARTS BIN" in sent["zpl"]


def test_confirmed_agent_plan_prints_location_label(tmp_path, monkeypatch) -> None:
    client = _app(tmp_path, zebra=ZebraConfig(host="printer.local"))
    client.post("/locations", json={"type": "box", "label": "Roaster Parts Bin"})
    sent = {}

    def fake_send(zpl, cfg):
        sent["zpl"] = zpl
        sent["host"] = cfg.host

    monkeypatch.setattr("searchable_storage_led.api.send_zpl", fake_send)

    resp = client.post(
        "/assistant/confirm",
        json={
            "command": "print a label for roaster parts",
            "plan": [
                {
                    "tool": "print_label",
                    "arguments": {"location_id": "roaster-parts-bin"},
                }
            ],
        },
    )

    assert resp.status_code == 200
    assert resp.json()["applied"][0]["printed_label"] == "roaster-parts-bin"
    assert "ROASTER PARTS BIN" in sent["zpl"]


def test_lights_enabled_follows_the_root_cabinet(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "cabinet", "label": "Chest", "parent_id": None})
    client.post("/locations", json={"type": "drawer", "label": "D", "parent_id": "chest"})

    # An unlit chest -> its drawer exposes no light config.
    assert client.get("/locations/d").json()["lights_enabled"] is False
    assert client.get("/locations/chest").json()["is_root"] is True

    # Turn lights on at the root -> descendants can now be mapped.
    client.post("/locations/chest/update", json={"light_target": "main"})
    assert client.get("/locations/d").json()["lights_enabled"] is True


def test_update_location_sets_light_binding(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "drawer", "label": "Lit Drawer", "parent_id": None})

    response = client.post(
        "/locations/lit-drawer/update",
        json={"light_target": "main", "light_indexes": [1, 0, 1]},
    )

    assert response.status_code == 200
    loc = client.get("/locations/lit-drawer").json()["location"]
    assert loc["light_target"] == "main"
    assert loc["light_indexes"] == [0, 1]
    assert loc["is_lit"] is True

    rejected = client.post(
        "/locations/lit-drawer/update",
        json={"light_indexes": [2]},
    )
    assert rejected.status_code == 400
    assert "configured range 0..1" in rejected.json()["detail"]
    assert client.get("/locations/lit-drawer").json()["location"]["light_indexes"] == [0, 1]


def test_location_hardware_binding_api_sets_wled_pixels(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "drawer", "label": "Lit Drawer", "parent_id": None})

    response = client.post(
        "/locations/lit-drawer/hardware-bindings",
        json={
            "kind": "wled_pixels",
            "controller_id": "main",
            "address": "0,1",
        },
    )

    assert response.status_code == 200
    assert response.json()["binding"]["kind"] == "wled_pixels"
    assert response.json()["binding"]["metadata"]["pixels"] == [0, 1]
    listed = client.get("/locations/lit-drawer/hardware-bindings").json()
    assert listed["bindings"][0]["address"] == "0,1"
    loc = client.get("/locations/lit-drawer").json()["location"]
    assert loc["hardware_bindings"][0]["kind"] == "wled_pixels"
    assert loc["light_target"] == "main"
    assert loc["light_indexes"] == [0, 1]
    assert loc["is_lit"] is True


def test_hardware_commissioning_preserves_layout_and_applies_atomically(tmp_path) -> None:
    client = _app(tmp_path)

    preview = client.post(
        "/locations/main-cabinet/hardware-bindings/batch-preview",
        json={"controller_id": "main-cabinet", "pixel_allocation": "existing_layout"},
    )

    assert preview.status_code == 200
    plan = preview.json()
    assert plan["schema"] == "magpie.hardware_commissioning.v1"
    assert plan["can_apply"] is True
    assert plan["summary"] == {
        "location_count": 2,
        "pixel_count": 2,
        "first_pixel": 0,
        "last_pixel": 1,
        "replacement_count": 0,
        "unchanged_count": 2,
    }
    expected = {
        assignment["location_id"]: assignment["pixels"]
        for assignment in plan["assignments"]
    }
    assert expected == {
        "B001": client.get("/locations/B001").json()["location"]["light_indexes"],
        "B002": client.get("/locations/B002").json()["location"]["light_indexes"],
    }

    applied = client.post(
        "/locations/main-cabinet/hardware-bindings/batch",
        json={"controller_id": "main-cabinet", "pixel_allocation": "existing_layout"},
    )

    assert applied.status_code == 200
    result = applied.json()
    assert result["action"] == "manual_bulk_hardware_binding"
    assert len(result["bindings"]) == 2
    assert isinstance(result["interaction_id"], int)
    for location_id, pixels in expected.items():
        binding = client.get(
            f"/locations/{location_id}/hardware-bindings"
        ).json()["bindings"][0]
        assert binding["metadata"]["pixels"] == pixels


def test_hardware_commissioning_requires_explicit_replacement_and_valid_range(
    tmp_path,
) -> None:
    client = _app(tmp_path)
    blocked = client.post(
        "/locations/main-cabinet/hardware-bindings/batch-preview",
        json={
            "controller_id": "main-cabinet",
            "pixel_allocation": "one",
            "start_pixel": 0,
            "direction": "reverse",
        },
    ).json()
    assert blocked["can_apply"] is False
    assert {row["code"] for row in blocked["blockers"]} == {"existing_binding"}

    replaceable = client.post(
        "/locations/main-cabinet/hardware-bindings/batch-preview",
        json={
            "controller_id": "main-cabinet",
            "pixel_allocation": "one",
            "start_pixel": 0,
            "direction": "reverse",
            "replace_existing": True,
        },
    ).json()
    assert replaceable["can_apply"] is True
    assert replaceable["summary"]["replacement_count"] == 2

    out_of_range = client.post(
        "/locations/main-cabinet/hardware-bindings/batch-preview",
        json={
            "controller_id": "main-cabinet",
            "pixel_allocation": "one",
            "start_pixel": 1,
            "direction": "reverse",
            "replace_existing": True,
        },
    ).json()
    assert out_of_range["can_apply"] is False
    assert "pixel_range" in {row["code"] for row in out_of_range["blockers"]}


def test_hardware_commissioning_rejects_pixels_owned_outside_subtree(tmp_path) -> None:
    client = _app(tmp_path)
    outside = client.post(
        "/locations",
        json={"type": "bin", "label": "Outside bin"},
    ).json()["id"]
    client.post(
        "/locations/B001/hardware-bindings",
        json={
            "kind": "wled_pixels",
            "controller_id": "main",
            "address": "0",
            "active": False,
        },
    )
    client.post(
        f"/locations/{outside}/hardware-bindings",
        json={
            "kind": "wled_pixels",
            "controller_id": "main",
            "address": "0",
        },
    )

    preview = client.post(
        "/locations/main-cabinet/hardware-bindings/batch-preview",
        json={"controller_id": "main", "pixel_allocation": "one"},
    ).json()

    assert preview["can_apply"] is False
    conflict = next(row for row in preview["blockers"] if row["code"] == "pixel_conflict")
    assert conflict["location_id"] == outside
    assert conflict["pixels"] == [0]


def test_location_update_replaces_and_disconnects_simple_light_binding(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "drawer", "label": "Lit Drawer", "parent_id": None})

    first = client.post(
        "/locations/lit-drawer/update",
        json={"light_target": "main", "light_indexes": [0]},
    )
    replacement = client.post(
        "/locations/lit-drawer/update",
        json={"light_target": "main", "light_indexes": [1]},
    )

    assert first.status_code == 200
    assert replacement.status_code == 200
    bindings = client.get("/locations/lit-drawer/hardware-bindings").json()["bindings"]
    assert [(binding["address"], binding["active"]) for binding in bindings] == [
        ("1", True),
        ("0", False),
    ]
    location = client.get("/locations/lit-drawer").json()["location"]
    assert location["light_target"] == "main"
    assert location["light_indexes"] == [1]

    disconnected = client.post(
        "/locations/lit-drawer/update",
        json={"light_target": "", "light_indexes": []},
    )

    assert disconnected.status_code == 200
    location = client.get("/locations/lit-drawer").json()["location"]
    assert location["light_target"] is None
    assert location["light_indexes"] == []
    assert location["is_lit"] is False
    bindings = client.get("/locations/lit-drawer/hardware-bindings").json()["bindings"]
    assert all(binding["active"] is False for binding in bindings)


def test_location_hardware_binding_keeps_unlit_child_resolution(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "drawer", "label": "Lit Drawer", "parent_id": None})
    client.post("/locations", json={"type": "pouch", "label": "Blue Pouch", "parent_id": "lit-drawer"})
    client.post(
        "/locations/lit-drawer/hardware-bindings",
        json={
            "kind": "wled_pixels",
            "controller_id": "main-cabinet",
            "address": "0",
        },
    )

    response = client.post("/locations/blue-pouch/light")

    assert response.status_code == 200
    assert response.json()["lit_location"] == "lit-drawer"


def test_hardware_diagnostics_reports_coverage_and_conflicts(tmp_path) -> None:
    client = _app(tmp_path)

    healthy = client.get("/hardware/diagnostics").json()

    assert healthy["schema"] == "magpie.hardware_diagnostics.v1"
    assert healthy["ready"] is True
    assert healthy["controller"] == {
        "backend": "wled",
        "pixel_count": 2,
        "currently_lit_pixels": 0,
    }
    assert healthy["configured_locations"] == 2
    assert healthy["assigned_pixels"] == 2
    assert healthy["conflicts"] == []

    response = client.post(
        "/locations/B002/hardware-bindings",
        json={
            "kind": "wled_pixels",
            "controller_id": "main-cabinet",
            "address": "0",
        },
    )
    assert response.status_code == 200

    conflict = client.get("/hardware/diagnostics").json()
    assert conflict["ready"] is False
    assert conflict["assigned_pixels"] == 1
    assert conflict["conflicts"] == [
        {
            "controller_id": "main-cabinet",
            "pixel": 0,
            "location_ids": ["B001", "B002"],
        }
    ]


def test_delete_location_removes_whole_subtree(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "shelf-unit", "label": "SU", "parent_id": None})
    client.post("/locations", json={"type": "shelf", "label": "S1", "parent_id": "su"})
    client.post("/locations", json={"type": "pouch", "label": "P", "parent_id": "s1"})

    resp = client.delete("/locations/su")
    assert resp.status_code == 200
    assert resp.json()["removed"] == 3
    assert client.get("/locations/su").status_code == 404
    assert client.get("/locations/p").status_code == 404


def test_restoring_parent_preserves_independently_archived_descendant(tmp_path) -> None:
    client = _app(tmp_path)
    client.post(
        "/locations",
        json={"type": "shelf-unit", "label": "SU", "parent_id": None},
    )
    client.post(
        "/locations",
        json={"type": "shelf", "label": "Retired", "parent_id": "su"},
    )
    client.post(
        "/locations",
        json={"type": "shelf", "label": "Current", "parent_id": "su"},
    )

    retired = client.post("/locations/retired/archive").json()
    archived_parent = client.post("/locations/su/archive").json()

    assert retired["archived_ids"] == ["retired"]
    assert archived_parent["archived_ids"] == ["su", "current"]
    preview = client.get(
        "/locations/su/removal-preview?include_archived=true"
    ).json()
    assert preview["subtree_ids"] == ["su", "current"]
    assert preview["location_count"] == 2

    restored = client.post("/locations/su/restore").json()

    assert restored["restored_ids"] == ["su", "current"]
    assert client.get("/locations/current").status_code == 200
    assert client.get("/locations/retired").status_code == 404
    retired_location = next(
        location
        for location in client.get("/locations?include_archived=true").json()[
            "locations"
        ]
        if location["id"] == "retired"
    )
    assert retired_location["active"] is False


def test_move_location_reparents_container_with_subtree_contents(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "shelf-unit", "label": "SU", "parent_id": None})
    client.post("/locations", json={"type": "shelf", "label": "S1", "parent_id": "su"})
    client.post("/locations", json={"type": "shelf", "label": "S2", "parent_id": "su"})
    client.post("/locations", json={"type": "pouch", "label": "P", "parent_id": "s1"})
    client.post("/locations/p/items", json={"name": "Allen keys", "quantity": 1, "unit": "set"})

    response = client.post("/locations/s1/move", json={"new_parent_id": "s2"})

    assert response.status_code == 200
    body = response.json()
    assert body["old_parent_id"] == "su"
    assert body["new_parent_id"] == "s2"
    assert body["subtree_ids"] == ["s1", "p"]
    moved = client.get("/locations/s1").json()
    assert moved["location"]["parent_id"] == "s2"
    assert moved["contents"][0]["name"] == "Allen keys"


def test_move_location_preview_reports_attached_storage_context_without_mutation(
    tmp_path,
) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "shelf-unit", "label": "SU", "parent_id": None})
    client.post("/locations", json={"type": "shelf", "label": "S1", "parent_id": "su"})
    client.post("/locations", json={"type": "shelf", "label": "S2", "parent_id": "su"})
    client.post("/locations", json={"type": "pouch", "label": "P", "parent_id": "s1"})
    item_id = client.post(
        "/locations/p/items",
        json={"name": "Allen keys", "quantity": 1, "unit": "set"},
    ).json()["item_id"]
    client.post(f"/inventory/{item_id}/home", json={"location_id": "p"})
    client.post(
        "/locations/p/hardware-bindings",
        json={
            "kind": "nfc_tag",
            "controller_id": "workshop-edge",
            "address": "tag-42",
        },
    )

    response = client.get("/locations/s1/move-preview?new_parent_id=s2")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == {
        "id": "s1",
        "label": "S1",
        "path": [
            {"id": "su", "label": "SU"},
            {"id": "s1", "label": "S1"},
        ],
    }
    assert body["destination"] == {
        "id": "s2",
        "label": "S2",
        "path": [
            {"id": "su", "label": "SU"},
            {"id": "s2", "label": "S2"},
        ],
    }
    assert body["impact"] == {
        "location_count": 2,
        "inventory_count": 1,
        "learned_home_count": 1,
        "placement_rule_count": 0,
        "hardware_binding_count": 1,
    }
    assert client.get("/locations/s1").json()["location"]["parent_id"] == "su"


def test_move_location_preview_rejects_noop_and_cycles(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "shelf-unit", "label": "SU", "parent_id": None})
    client.post("/locations", json={"type": "shelf", "label": "S1", "parent_id": "su"})
    client.post("/locations", json={"type": "pouch", "label": "P", "parent_id": "s1"})

    unchanged = client.get("/locations/s1/move-preview?new_parent_id=su")
    cycle = client.get("/locations/s1/move-preview?new_parent_id=p")

    assert unchanged.status_code == 409
    assert unchanged.json()["detail"] == "location is already stored at that destination"
    assert cycle.status_code == 400
    assert "own subtree" in cycle.json()["detail"]
    assert client.get("/locations/s1").json()["location"]["parent_id"] == "su"


def test_manual_location_move_can_be_undone(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "shelf-unit", "label": "SU", "parent_id": None})
    client.post("/locations", json={"type": "shelf", "label": "S1", "parent_id": "su"})
    client.post("/locations", json={"type": "shelf", "label": "S2", "parent_id": "su"})

    moved = client.post("/locations/s1/move", json={"new_parent_id": "s2"}).json()

    assert moved["action"] == "manual_move_location"
    assert moved["interaction_id"] > 0
    assert moved["applied"][0]["moved_container"] == "s1"
    assert client.get("/locations/s1").json()["location"]["parent_id"] == "s2"

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    assert undone["undone"][0]["moved_container"] == "s1"
    assert client.get("/locations/s1").json()["location"]["parent_id"] == "su"


def test_location_history_endpoint_lists_recent_container_events(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "shelf-unit", "label": "SU", "parent_id": None})
    client.post("/locations", json={"type": "shelf", "label": "S1", "parent_id": "su"})
    client.post("/locations", json={"type": "shelf", "label": "S2", "parent_id": "su"})

    client.post("/locations/s1/move", json={"new_parent_id": "s2"})
    resp = client.get("/locations/s1/history")

    assert resp.status_code == 200
    body = resp.json()
    assert body["location"]["label"] == "S1"
    assert [event["event_type"] for event in body["events"][:2]] == [
        "location_moved",
        "location_created",
    ]
    assert body["events"][0]["location_id"] == "s1"
    assert body["events"][0]["note"] == "from su to s2"

    client.post("/locations/s1/archive")
    archived = client.get("/locations/s1/history")

    assert archived.status_code == 200
    assert archived.json()["location"]["active"] is False
    assert archived.json()["events"][0]["event_type"] == "location_archived"


def test_move_location_rejects_cycles(tmp_path) -> None:
    client = _app(tmp_path)
    client.post("/locations", json={"type": "shelf-unit", "label": "SU", "parent_id": None})
    client.post("/locations", json={"type": "shelf", "label": "S1", "parent_id": "su"})
    client.post("/locations", json={"type": "pouch", "label": "P", "parent_id": "s1"})

    response = client.post("/locations/s1/move", json={"new_parent_id": "p"})

    assert response.status_code == 400
    assert "own subtree" in response.text
    assert client.get("/locations/s1").json()["location"]["parent_id"] == "su"


def test_create_location_rejects_unknown_parent(tmp_path) -> None:
    client = _app(tmp_path)
    bad = client.post("/locations", json={"type": "pouch", "label": "Orphan", "parent_id": "nope"})
    assert bad.status_code == 400


def test_cabinet_label_is_humanized(tmp_path) -> None:
    tree = _app(tmp_path).get("/locations").json()
    cabinet = next(loc for loc in tree["locations"] if loc["id"] == "main-cabinet")
    assert cabinet["label"] == "Test"  # layout name "test" -> titled
