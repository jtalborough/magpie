import sqlite3
import threading
from dataclasses import replace

import pytest

import searchable_storage_led.locations as locations_module

from searchable_storage_led.layout import StorageLayout
from searchable_storage_led.locations import (
    Location,
    LocationNameConflictError,
    LocationStore,
    backfill_from_layout,
    default_accepts_items,
    generate_cells,
    location_path,
    match_locations,
    normalize_location_type,
)


def test_location_destination_defaults_distinguish_organizers_from_containers() -> None:
    for location_type in ("home", "room", "area", "closet", "cabinet", "rack", "storage_unit"):
        assert default_accepts_items(location_type) is False
    for location_type in ("shelf", "drawer", "tray", "bag", "organizer", "tote", "box", "bin", "pouch", "locker", "cell"):
        assert default_accepts_items(location_type) is True


def test_location_type_vocabulary_is_normalized_and_closed() -> None:
    assert normalize_location_type(" Organizer ") == "organizer"
    assert normalize_location_type("shelf-unit") == "storage_unit"
    with pytest.raises(ValueError, match="unknown storage place type"):
        normalize_location_type("spaceship")


def test_accepts_items_round_trips_and_migrates_existing_rows(tmp_path) -> None:
    store = LocationStore(tmp_path / "inventory.db")
    store.upsert(
        Location(
            id="organizer",
            type="cabinet",
            label="Organizer",
            accepts_items=False,
        )
    )

    assert store.get("organizer").accepts_items is False
    assert store.get("organizer").as_dict()["accepts_items"] is False


def test_generate_cells_creates_positioned_grid(tmp_path) -> None:
    store = LocationStore(tmp_path / "inventory.db")
    store.upsert(
        Location(id="drawer-x", type="drawer", parent_id=None, label="Drawer X",
                 grid={"cols": 2, "rows": 2})
    )

    ids = generate_cells(store, "drawer-x", 2, 2)

    assert len(ids) == 4
    assert len(store.children("drawer-x")) == 4
    cell = store.get("drawer-x-c2-r1")
    assert cell.col == 2 and cell.row == 1 and cell.w == 1 and cell.h == 1
    # Cells get memorable word labels, unique within the drawer (not "C2·R1").
    labels = [store.get(i).label for i in ids]
    assert all(label.isalpha() or "-" in label for label in labels)
    assert len(set(labels)) == 4


def test_position_and_footprint_round_trip(tmp_path) -> None:
    store = LocationStore(tmp_path / "inventory.db")
    store.upsert(
        Location(id="wide-drawer", type="drawer", parent_id=None, label="Wide",
                 col=2, row=3, w=3, h=2)
    )
    loc = store.get("wide-drawer")
    assert (loc.col, loc.row, loc.w, loc.h) == (2, 3, 3, 2)


def _backfilled(tmp_path):
    layout = StorageLayout.from_path("config/storage-layout.json")
    store = LocationStore(tmp_path / "inventory.db")
    backfill_from_layout(store, layout)
    return layout, store


def test_backfill_creates_cabinet_root_and_all_bins(tmp_path) -> None:
    layout, store = _backfilled(tmp_path)

    root = store.get("main-cabinet")
    assert root is not None
    assert root.parent_id is None
    assert root.type == "cabinet"
    assert root.light_target == "main-cabinet"
    assert root.accepts_items is False
    assert len(store.children("main-cabinet")) == len(layout.bins) == 128
    assert store.count() == 129  # cabinet + 128 bins


def test_bins_keep_exact_layout_led_indexes(tmp_path) -> None:
    layout, store = _backfilled(tmp_path)

    # Same bins asserted in test_layout.py — the cabinet must light identically.
    for bin_id in ("B001", "B014", "B015", "B112", "B128"):
        assert store.get(bin_id).light_indexes == layout.get(bin_id).led_indexes
        assert store.get(bin_id).light_target == "main-cabinet"
    assert store.get("B001").is_lit is True


def test_every_bin_parents_to_the_cabinet(tmp_path) -> None:
    layout, store = _backfilled(tmp_path)

    for bin in layout.bins:
        assert store.get(bin.id).parent_id == "main-cabinet"
        assert store.get(bin.id).accepts_items is True


def test_ancestors_and_nearest_lit_for_a_bin(tmp_path) -> None:
    _, store = _backfilled(tmp_path)

    assert [loc.id for loc in store.ancestors("B001")] == ["B001", "main-cabinet"]
    # A lit bin is its own nearest lit location.
    assert store.nearest_lit("B001").id == "B001"


def test_unlit_child_degrades_to_nearest_lit_ancestor(tmp_path) -> None:
    """The graceful-degradation primitive: an unlit pouch in a lit bin still
    resolves to a light (the bin) plus a words path."""
    _, store = _backfilled(tmp_path)

    store.upsert(
        Location(id="blue-pouch", type="pouch", parent_id="B001", label="Blue Pouch")
    )

    pouch = store.get("blue-pouch")
    assert pouch.is_lit is False
    assert store.nearest_lit("blue-pouch").id == "B001"
    assert [loc.id for loc in store.ancestors("blue-pouch")] == [
        "blue-pouch",
        "B001",
        "main-cabinet",
    ]


def test_location_matching_requires_or_uses_path_for_duplicate_labels(tmp_path) -> None:
    store = LocationStore(tmp_path / "inventory.db")
    for location in (
        Location(id="basement", type="room", label="Basement"),
        Location(id="garage", type="room", label="Garage"),
        Location(
            id="printer-cabinet",
            type="cabinet",
            parent_id="basement",
            label="Printer Cabinet",
        ),
        Location(
            id="printer-cabinet-2",
            type="cabinet",
            parent_id="garage",
            label="Printer Cabinet",
        ),
    ):
        store.upsert(location)

    assert {row.id for row in match_locations("Printer Cabinet", store)} == {
        "printer-cabinet",
        "printer-cabinet-2",
    }
    assert [row.id for row in match_locations("Garage > Printer Cabinet", store)] == [
        "printer-cabinet-2"
    ]
    assert [row.id for row in match_locations("Printer Cabinet in Garage", store)] == [
        "printer-cabinet-2"
    ]
    assert [row.id for row in match_locations("printer-cabinet", store)] == [
        "printer-cabinet"
    ]
    assert [row.label for row in location_path(store, "printer-cabinet-2")] == [
        "Garage",
        "Printer Cabinet",
    ]


def test_location_events_show_recent_container_history(tmp_path) -> None:
    store = LocationStore(tmp_path / "inventory.db")
    store.upsert(Location(id="bench", type="shelf", label="Bench"))
    store.record_event(
        event_type="location_created",
        location_id="bench",
        note="manual shelf",
    )
    store.record_event(
        event_type="location_moved",
        location_id="bench",
        note="from shop to garage",
    )

    events = store.events("bench")

    assert [event["event_type"] for event in events[:2]] == [
        "location_moved",
        "location_created",
    ]
    assert events[0]["location_id"] == "bench"
    assert events[0]["note"] == "from shop to garage"
    assert events[0]["created_at"]
    assert store.has_event("location_created") is True
    assert store.has_event("location_moved", location_id="bench") is True
    assert store.has_event("location_moved", location_id="missing") is False
    assert store.has_event("") is False


def test_latest_location_events_returns_one_per_place_and_honest_truncation(
    tmp_path,
) -> None:
    store = LocationStore(tmp_path / "inventory.db")
    for location_id in ("bench", "drawer"):
        store.upsert(Location(id=location_id, type="shelf", label=location_id))
        store.record_event(
            event_type="location_contents_verified",
            location_id=location_id,
            quantity=1,
            note=f"older {location_id}",
        )
    store.record_event(
        event_type="location_contents_verified",
        location_id="bench",
        quantity=2,
        note="newer bench",
    )

    events, truncated = store.latest_events_by_location(
        "location_contents_verified",
    )
    limited, limited_truncated = store.latest_events_by_location(
        "location_contents_verified",
        limit=1,
    )

    assert truncated is False
    assert set(events) == {"bench", "drawer"}
    assert events["bench"]["quantity"] == 2
    assert events["bench"]["note"] == "newer bench"
    assert limited_truncated is True
    assert list(limited) == ["bench"]


def test_backfill_is_idempotent(tmp_path) -> None:
    layout = StorageLayout.from_path("config/storage-layout.json")
    store = LocationStore(tmp_path / "inventory.db")

    backfill_from_layout(store, layout)
    backfill_from_layout(store, layout)

    assert store.count() == 129
    assert store.get("B001").light_indexes == layout.get("B001").led_indexes


def test_archive_and_restore_subtree_preserves_nodes(tmp_path) -> None:
    store = LocationStore(tmp_path / "inventory.db")
    store.upsert(Location(id="attic", type="area", label="Attic"))
    store.upsert(Location(id="tote", type="tote", parent_id="attic", label="Tote"))

    assert store.archive_subtree("attic") == ["attic", "tote"]
    assert store.get("attic") is None
    assert store.get("tote") is None
    assert store.count() == 0
    assert store.count(include_archived=True) == 2
    assert store.get_any("attic").active is False
    assert store.get_any("attic").archived_at

    assert store.restore_subtree("attic") == ["attic", "tote"]
    assert store.get("attic").active is True
    assert store.get("tote").active is True
    assert store.get("attic").archived_at is None


def test_repository_rejects_normalized_duplicate_active_sibling_names(tmp_path) -> None:
    store = LocationStore(tmp_path / "inventory.db")
    store.upsert(Location(id="garage", type="room", label="Garage"))
    store.upsert(
        Location(
            id="parts-cabinet",
            type="cabinet",
            parent_id="garage",
            label="Parts Cabinet",
        )
    )

    with pytest.raises(LocationNameConflictError, match="already exists"):
        store.upsert(
            Location(
                id="parts-cabinet-2",
                type="cabinet",
                parent_id="garage",
                label="  parts   CABINET ",
            )
        )

    store.upsert(
        Location(id="parts-cabinet-elsewhere", type="cabinet", label="Parts Cabinet")
    )
    assert store.get("parts-cabinet-elsewhere") is not None


def test_repository_serializes_concurrent_duplicate_location_writes(tmp_path) -> None:
    store = LocationStore(tmp_path / "inventory.db")
    store.upsert(Location(id="garage", type="room", label="Garage"))
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def create(location_id: str) -> None:
        barrier.wait()
        try:
            store.upsert(
                Location(
                    id=location_id,
                    type="cabinet",
                    parent_id="garage",
                    label="Parts Cabinet",
                )
            )
        except LocationNameConflictError:
            outcomes.append("conflict")
        else:
            outcomes.append("created")

    threads = [
        threading.Thread(target=create, args=("parts-a",)),
        threading.Thread(target=create, args=("parts-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["conflict", "created"]
    assert [row.label for row in store.children("garage")] == ["Parts Cabinet"]


def test_repository_upsert_preserves_location_graph_invariants(tmp_path) -> None:
    store = LocationStore(tmp_path / "inventory.db")
    store.upsert(Location(id="garage", type="room", label="Garage"))
    store.upsert(Location(id="cabinet", type="cabinet", parent_id="garage"))
    store.upsert(Location(id="drawer", type="drawer", parent_id="cabinet"))

    with pytest.raises(KeyError, match="unknown parent"):
        store.upsert(Location(id="lost-box", type="box", parent_id="missing"))
    with pytest.raises(ValueError, match="own parent"):
        store.upsert(Location(id="self", type="box", parent_id="self"))
    with pytest.raises(ValueError, match="own subtree"):
        store.upsert(replace(store.get("garage"), parent_id="drawer"))

    assert store.get("garage").parent_id is None


def test_repository_create_many_rolls_back_a_partial_structure(tmp_path) -> None:
    store = LocationStore(tmp_path / "inventory.db")

    with pytest.raises(LocationNameConflictError, match="already exists"):
        store.create_many(
            [
                Location(id="garage", type="room", label="Garage"),
                Location(
                    id="parts-a",
                    type="cabinet",
                    parent_id="garage",
                    label="Parts",
                ),
                Location(
                    id="parts-b",
                    type="cabinet",
                    parent_id="garage",
                    label="  PARTS  ",
                ),
            ]
        )

    assert store.all(include_archived=True) == []


def test_repository_create_many_uses_one_database_transaction(
    tmp_path, monkeypatch
) -> None:
    store = LocationStore(tmp_path / "inventory.db")
    original_connect = store.connect
    connection_count = 0

    def tracked_connect():
        nonlocal connection_count
        connection_count += 1
        return original_connect()

    monkeypatch.setattr(store, "connect", tracked_connect)

    assert store.create_many(
        [
            Location(id="workshop", type="room", label="Workshop"),
            Location(
                id="parts-cabinet",
                type="cabinet",
                parent_id="workshop",
                label="Parts cabinet",
            ),
            Location(
                id="small-parts",
                type="drawer",
                parent_id="parts-cabinet",
                label="Small parts",
            ),
        ]
    ) == ["workshop", "parts-cabinet", "small-parts"]
    assert connection_count == 1
    assert store.get("small-parts").parent_id == "parts-cabinet"


def test_repository_only_allows_archived_children_under_archived_parents(tmp_path) -> None:
    store = LocationStore(tmp_path / "inventory.db")
    store.upsert(Location(id="attic", type="area", active=False, archived_at="now"))

    with pytest.raises(ValueError, match="parent location is archived"):
        store.upsert(Location(id="active-box", type="box", parent_id="attic"))

    store.upsert(
        Location(
            id="archived-box",
            type="box",
            parent_id="attic",
            active=False,
            archived_at="now",
        )
    )
    assert store.get_any("archived-box").parent_id == "attic"


def test_repository_allows_non_identity_updates_to_legacy_duplicate_rows(tmp_path) -> None:
    store = LocationStore(tmp_path / "inventory.db")
    with sqlite3.connect(store.path) as conn:
        conn.executemany(
            """
            INSERT INTO locations(id, parent_id, type, label, capacity_class, active)
            VALUES (?, NULL, 'cabinet', 'Main Cabinet', 'medium', 1)
            """,
            [("main-cabinet",), ("main-cabinet-2",)],
        )

    existing = store.get("main-cabinet")
    store.upsert(replace(existing, capacity_class="large"))

    assert store.get("main-cabinet").capacity_class == "large"
    assert store.get("main-cabinet-2").active is True


def test_restore_rejects_a_replacement_with_the_same_sibling_name(tmp_path) -> None:
    store = LocationStore(tmp_path / "inventory.db")
    store.upsert(Location(id="garage", type="room", label="Garage"))
    store.upsert(
        Location(
            id="parts-bin",
            type="bin",
            parent_id="garage",
            label="Parts",
        )
    )
    store.archive_subtree("parts-bin")
    store.upsert(
        Location(
            id="replacement-parts-bin",
            type="bin",
            parent_id="garage",
            label="Parts",
        )
    )

    with pytest.raises(LocationNameConflictError, match="already exists"):
        store.restore_subtree("parts-bin")

    assert store.get("parts-bin") is None
    assert store.get("replacement-parts-bin") is not None


def test_restore_child_requires_active_parent(tmp_path) -> None:
    store = LocationStore(tmp_path / "inventory.db")
    store.upsert(Location(id="attic", type="area"))
    store.upsert(Location(id="tote", type="tote", parent_id="attic"))
    store.archive_subtree("attic")

    with pytest.raises(ValueError, match="parent"):
        store.restore_subtree("tote")


def test_layout_backfill_preserves_archived_cabinet_state(tmp_path) -> None:
    layout, store = _backfilled(tmp_path)
    store.archive_subtree("main-cabinet")

    backfill_from_layout(store, layout)

    assert store.get("main-cabinet") is None
    assert store.get("B001") is None
    assert store.get_any("main-cabinet").active is False


def test_grid_and_light_round_trip(tmp_path) -> None:
    store = LocationStore(tmp_path / "inventory.db")
    store.upsert(
        Location(
            id="drawer-2",
            type="drawer",
            parent_id=None,
            label="Drawer 2",
            grid={"cols": 7, "rows": 9},
            light_target="drawer-strip",
            light_indexes=[3, 4, 5],
        )
    )

    loc = store.get("drawer-2")
    assert loc.grid == {"cols": 7, "rows": 9}
    assert loc.light_indexes == [3, 4, 5]
    assert loc.light_target == "drawer-strip"
    assert loc.is_lit is True


def test_hardware_binding_round_trip_and_light_compatibility(tmp_path) -> None:
    store = LocationStore(tmp_path / "inventory.db")
    store.upsert(Location(id="drawer-3", type="drawer", label="Drawer 3"))

    binding = store.set_hardware_binding(
        location_id="drawer-3",
        kind="wled_pixels",
        controller_id="main",
        address="9, 10, 11",
        metadata={"pixels": [9, 10, 11]},
    )

    assert binding.kind == "wled_pixels"
    assert binding.metadata["pixels"] == [9, 10, 11]
    assert store.hardware_bindings("drawer-3")[0].address == "9, 10, 11"
    loc = store.get("drawer-3")
    assert loc.light_target == "main"
    assert loc.light_indexes == [9, 10, 11]
    assert store.nearest_lit("drawer-3").id == "drawer-3"

    replacement = store.set_hardware_binding(
        location_id="drawer-3",
        kind="wled_pixels",
        controller_id="main",
        address="12, 13",
        metadata={"pixels": [12, 13]},
    )

    bindings = store.hardware_bindings("drawer-3")
    assert replacement.active is True
    assert [(entry.address, entry.active) for entry in bindings] == [
        ("12, 13", True),
        ("9, 10, 11", False),
    ]
    assert store.get("drawer-3").light_indexes == [12, 13]


def test_hardware_binding_batch_is_atomic(tmp_path, monkeypatch) -> None:
    store = LocationStore(tmp_path / "inventory.db")
    store.upsert(Location(id="B001", type="bin"))
    store.upsert(Location(id="B002", type="bin"))
    original_upsert = locations_module._upsert_hardware_binding
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated second binding failure")
        return original_upsert(*args, **kwargs)

    monkeypatch.setattr(locations_module, "_upsert_hardware_binding", fail_second)

    with pytest.raises(RuntimeError, match="second binding failure"):
        store.set_hardware_bindings(
            [
                {
                    "location_id": "B001",
                    "kind": "wled_pixels",
                    "controller_id": "main",
                    "address": "0",
                    "metadata": {"pixels": [0]},
                },
                {
                    "location_id": "B002",
                    "kind": "wled_pixels",
                    "controller_id": "main",
                    "address": "1",
                    "metadata": {"pixels": [1]},
                },
            ]
        )

    assert store.hardware_bindings("B001") == []
    assert store.hardware_bindings("B002") == []
    assert store.get("B001").light_indexes == []
    assert store.get("B002").light_indexes == []
