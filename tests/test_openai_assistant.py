import base64

import pytest

from searchable_storage_led.layout import StorageLayout
from searchable_storage_led.llm_client import LlmError
from searchable_storage_led.openai_assistant import (
    normalize_openai_decision,
    parse_response_json,
    photo_data_url,
    prompt_size_profiles,
)
from searchable_storage_led.store import InventoryStore, PhotoRecord


def small_layout() -> StorageLayout:
    return StorageLayout.from_spec(
        {
            "name": "test",
            "columns": 2,
            "rows_bottom_to_top": [
                {"size": "small", "bin_count": 2, "spans": [1, 1]},
            ],
        }
    )


def mixed_size_layout() -> StorageLayout:
    return StorageLayout.from_spec(
        {
            "name": "test",
            "columns": 2,
            "rows_bottom_to_top": [
                {"size": "small", "bin_count": 2, "spans": [1, 1]},
                {"size": "medium", "bin_count": 1, "spans": [2]},
            ],
        }
    )


def test_parse_response_json_uses_output_text() -> None:
    parsed = parse_response_json(
        {
            "output_text": (
                '{"action":"store_item","message":"ok","query":"m3 screws",'
                '"item_draft":null}'
            )
        }
    )

    assert parsed["action"] == "store_item"
    assert parsed["query"] == "m3 screws"


def test_photo_data_url_uses_repository_content_for_remote_photos() -> None:
    photo = PhotoRecord(
        id=17,
        filename="part.jpg",
        content_type="image/jpeg",
        path="workspace/object-storage-path.jpg",
        url="/photos/17",
    )

    result = photo_data_url(photo, lambda photo_id: b"image" if photo_id == 17 else b"")

    assert result == "data:image/jpeg;base64," + base64.b64encode(b"image").decode("ascii")


def test_photo_data_url_surfaces_repository_download_failures() -> None:
    photo = PhotoRecord(
        id=18,
        filename="part.jpg",
        content_type="image/jpeg",
        path="workspace/missing.jpg",
        url="/photos/18",
    )

    def unavailable(_photo_id: int) -> bytes:
        raise RuntimeError("object storage unavailable")

    with pytest.raises(LlmError, match="photo 18 could not be loaded"):
        photo_data_url(photo, unavailable)


def test_openai_store_suggestion_is_validated_against_empty_bins(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="Existing item")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")

    decision = normalize_openai_decision(
        {
            "action": "store_item",
            "message": "Put it away.",
            "query": "dupont jumper wires",
            "item_draft": {
                "name": "Dupont Jumper Wires",
                "category": "electronics",
                "required_bin_size": "small",
                "aliases": ["jumper wires"],
                "description": "Small connector wires.",
                "quantity": 1,
                "unit": "bundle",
            },
        },
        text="put these away",
        layout=small_layout(),
        store=store,
    )

    assert decision["action"] == "store_item"
    assert decision["suggested_bin"] == "B002"
    assert decision["item_draft"]["name"] == "Dupont Jumper Wires"
    assert decision["item_draft"]["unit"] == "bundle"


def test_openai_required_size_overrides_small_bin_default(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")

    decision = normalize_openai_decision(
        {
            "action": "store_item",
            "message": "Put it away.",
            "query": "heat shrink tubing assortment",
            "item_draft": {
                "name": "Heat Shrink Tubing Assortment",
                "category": "electronics",
                "required_bin_size": "medium",
                "aliases": ["shrink wrap"],
                "description": "A bundled assortment of heat shrink tubing.",
                "quantity": 1,
                "unit": "pack",
            },
        },
        text="put this away",
        layout=mixed_size_layout(),
        store=store,
    )

    assert decision["action"] == "store_item"
    assert decision["suggested_bin"] == "B003"
    assert decision["item_draft"]["required_bin_size"] == "medium"


def test_openai_prompt_size_profiles_include_fit_rules() -> None:
    layout = StorageLayout.from_path("config/storage-layout.json")

    profiles = prompt_size_profiles(
        layout.size_profiles,
        [
            {"id": "B001", "size": "small"},
            {"id": "B113", "size": "medium"},
            {"id": "B127", "size": "large"},
        ],
    )

    assert profiles["small"]["relative_size"] == "1 cabinet width unit"
    assert profiles["medium"]["relative_size"] == "3-4 cabinet width units"
    assert profiles["large"]["relative_size"] == "7 cabinet width units"
    assert "actual object group" in profiles["medium"]["fit_rule"]
    assert profiles["large"]["candidate_empty_count_in_prompt"] == 1
