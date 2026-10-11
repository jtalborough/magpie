from searchable_storage_led.assistant import (
    assistant_decision,
    relaxed_find_queries,
    search_inventory,
)
from searchable_storage_led.layout import StorageLayout
from searchable_storage_led.store import InventoryStore


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
                {"size": "large", "bin_count": 1, "spans": [2]},
            ],
        }
    )


def test_need_command_finds_existing_inventory(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="Heat shrink tubing", aliases=["shrink wrap"])
    store.put_inventory(item_id=item_id, bin_id="B002", quantity=1, unit="roll")

    decision = assistant_decision(
        "I need shrink wrap",
        layout=small_layout(),
        store=store,
    )

    assert decision["action"] == "find_item"
    assert decision["suggested_bin"] == "B002"
    assert decision["results"][0]["name"] == "Heat shrink tubing"


def test_find_relaxes_conversational_container_wording_after_exact_miss(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="M3 machine screws")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=20, unit="each")

    results = search_inventory(store, "the box of M3 screws")

    assert [result.name for result in results] == ["M3 machine screws"]


def test_find_preserves_exact_container_name_before_relaxing(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    exact_id = store.upsert_item(name="Box of M3 screws")
    loose_id = store.upsert_item(name="M3 screws")
    store.put_inventory(item_id=exact_id, bin_id="B001", quantity=1, unit="box")
    store.put_inventory(item_id=loose_id, bin_id="B002", quantity=20, unit="each")

    results = search_inventory(store, "box of M3 screws")

    assert [result.name for result in results] == ["Box of M3 screws"]


def test_find_matches_plural_query_to_singular_alias(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="Allen Wrench", aliases=["allen key"])
    store.put_inventory(item_id=item_id, bin_id="B114", quantity=1, unit="each")

    results = search_inventory(store, "Allen keys")

    assert [result.name for result in results] == ["Allen Wrench"]


def test_find_matches_es_plural_query_to_singular_name(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="Allen Wrench")
    store.put_inventory(item_id=item_id, bin_id="B114", quantity=1, unit="each")

    results = search_inventory(store, "Allen wrenches")

    assert [result.name for result in results] == ["Allen Wrench"]
    assert relaxed_find_queries("Allen wrenches") == [
        "allen wrenches",
        "allen wrench",
    ]


def test_find_does_not_invent_an_absent_singular_item(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="Hot glue gun")
    store.put_inventory(item_id=item_id, bin_id="B020", quantity=1, unit="each")

    assert search_inventory(store, "glue sticks") == []


def test_put_away_command_drafts_item_for_empty_bin(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    layout = small_layout()
    item_id = store.upsert_item(name="Existing item")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")

    decision = assistant_decision(
        "put this allen key away",
        layout=layout,
        store=store,
    )

    assert decision["action"] == "store_item"
    assert decision["suggested_bin"] == "B002"
    assert decision["item_draft"]["name"] == "Allen Key"
    assert decision["item_draft"]["category"] == "tools"


def test_put_away_command_uses_medium_bin_for_packed_items(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")

    decision = assistant_decision(
        "put this pack of heat shrink wrap away",
        layout=mixed_size_layout(),
        store=store,
    )

    assert decision["action"] == "store_item"
    assert decision["suggested_bin"] == "B003"
    assert decision["item_draft"]["required_bin_size"] == "medium"
