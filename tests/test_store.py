import sqlite3

import pytest

from searchable_storage_led.locations import Location, LocationStore
from searchable_storage_led.store import InventoryAuditConflictError, InventoryStore


def test_inventory_items_can_reference_photos(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    photo = store.add_photo(
        filename="screw.jpg",
        content_type="image/jpeg",
        data=b"image-data",
    )

    item_id = store.upsert_item(
        name="M5 pan head screw",
        category="fasteners",
        aliases=["panhead"],
        photo_ids=[photo.id],
    )
    store.put_inventory(item_id=item_id, bin_id="1", quantity=12, unit="each")

    results = store.search("panhead")

    assert results[0].bin_id == "B001"
    assert results[0].photos == [photo.url]
    assert (tmp_path / "photos").exists()


def test_atomic_inventory_import_rolls_back_new_locations_and_prior_rows(
    tmp_path,
) -> None:
    path = tmp_path / "inventory.db"
    store = InventoryStore(path)
    locations = LocationStore(path)

    with pytest.raises(ValueError, match="unknown or archived location"):
        store.import_inventory_rows_atomic(
            new_locations=[
                Location(
                    id="garage",
                    type="room",
                    label="Garage",
                    accepts_items=False,
                ),
                Location(
                    id="garage-parts",
                    type="bin",
                    parent_id="garage",
                    label="Parts",
                ),
            ],
            rows=[
                {
                    "name": "M4 T-nuts",
                    "location_id": "garage-parts",
                    "quantity": 12,
                    "unit": "each",
                    "aliases": [],
                },
                {
                    "name": "USB cable",
                    "location_id": "missing-place",
                    "quantity": 1,
                    "unit": "each",
                    "aliases": [],
                },
            ],
        )

    assert locations.get_any("garage") is None
    assert locations.get_any("garage-parts") is None
    assert store.search("M4 T-nuts") == []


def test_location_audit_rejects_changed_snapshot_without_partial_updates(
    tmp_path,
) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    first = store.upsert_item(name="M4 washers")
    second = store.upsert_item(name="M4 T-nuts")
    store.put_inventory(
        item_id=first,
        bin_id="fasteners",
        quantity=20,
        unit="each",
        needs_review=True,
        review_reason="Old uncertainty",
        normalize_bin=False,
    )
    store.put_inventory(
        item_id=second,
        bin_id="metric",
        quantity=8,
        unit="each",
        normalize_bin=False,
    )

    with pytest.raises(
        InventoryAuditConflictError,
        match="inventory changed while this contents check was open",
    ):
        store.apply_location_audit(
            location_id="fasteners",
            location_ids=["fasteners", "metric"],
            placements=[
                {
                    "item_id": first,
                    "location_id": "fasteners",
                    "expected_quantity": 20,
                    "expected_unit": "each",
                    "expected_needs_review": True,
                    "status": "present",
                },
                {
                    "item_id": second,
                    "location_id": "metric",
                    "expected_quantity": 99,
                    "expected_unit": "each",
                    "expected_needs_review": False,
                    "status": "missing",
                },
            ],
            verification_note='{"schema":"magpie.location_verification.v2"}',
        )

    [still_uncertain] = store.review_queue()
    assert still_uncertain.item_id == first
    assert LocationStore(tmp_path / "inventory.db").latest_event(
        "location_contents_verified",
        location_id="fasteners",
    ) is None


def test_location_content_revisions_follow_inventory_and_hierarchy_changes(
    tmp_path,
) -> None:
    path = tmp_path / "inventory.db"
    store = InventoryStore(path)
    locations = LocationStore(path)
    locations.upsert(Location(id="root", type="cabinet", label="Root"))
    locations.upsert(
        Location(id="first", type="drawer", label="First", parent_id="root")
    )
    locations.upsert(
        Location(id="second", type="drawer", label="Second", parent_id="root")
    )
    baseline = store.location_inventory_metrics()

    item_id = store.upsert_item(name="M4 washers")
    store.put_inventory(
        item_id=item_id,
        bin_id="first",
        quantity=20,
        unit="each",
        normalize_bin=False,
    )
    after_put = store.location_inventory_metrics()
    assert after_put["first"]["content_revision"] > baseline["first"]["content_revision"]
    assert after_put["root"]["content_revision"] > baseline["root"]["content_revision"]
    assert after_put["first"]["item_count"] == 1

    store.move_inventory(
        item_id=item_id,
        from_bin="first",
        to_bin="second",
        normalize_bin=False,
    )
    after_move = store.location_inventory_metrics()
    assert after_move["first"]["content_revision"] > after_put["first"]["content_revision"]
    assert after_move["second"]["content_revision"] > after_put["second"]["content_revision"]
    assert after_move["root"]["content_revision"] > after_put["root"]["content_revision"]
    assert after_move["first"]["item_count"] == 0
    assert after_move["second"]["item_count"] == 1

    locations.move("second", "first")
    after_hierarchy_move = store.location_inventory_metrics()
    assert after_hierarchy_move["first"]["content_revision"] > after_move["first"]["content_revision"]
    assert after_hierarchy_move["second"]["content_revision"] > after_move["second"]["content_revision"]
    assert after_hierarchy_move["root"]["content_revision"] > after_move["root"]["content_revision"]
    assert LocationStore(tmp_path / "inventory.db").latest_event(
        "location_contents_verified",
        location_id="metric",
    ) is None


def test_item_name_dimensions_are_inferred_and_follow_inferred_rename(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="M4 x 12 mm button head screws")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=4, unit="pieces")

    [created] = store.item_inventory(item_id)
    assert created.dimensions == {"length_mm": 12.0, "thread_diameter_mm": 4.0}

    store.update_item(item_id=item_id, name="M4 x 20 mm button head screws")

    [renamed] = store.item_inventory(item_id)
    assert renamed.dimensions == {"length_mm": 20.0, "thread_diameter_mm": 4.0}


def test_item_rename_preserves_user_measured_dimensions(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="M4 x 12 mm screws")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=4, unit="pieces")
    store.update_item_size(
        item_id=item_id,
        size_class="small",
        dimensions={"length_mm": 12.2, "thread_diameter_mm": 4.1},
    )

    store.update_item(item_id=item_id, name="M4 x 20 mm screws")

    [renamed] = store.item_inventory(item_id)
    assert renamed.dimensions == {"length_mm": 12.2, "thread_diameter_mm": 4.1}


def test_item_size_metadata_can_be_restored_exactly(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="Camera case")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")
    store.update_item_size(
        item_id=item_id,
        size_class="large",
        source="agent",
        confidence=1,
        dimensions={"length_mm": 304.8, "width_mm": 203.2},
    )

    store.restore_item_size_metadata(
        item_id=item_id,
        size_class="small",
        source="user",
        confidence=0.75,
        dimensions={"length_mm": 100, "width_mm": 80},
    )

    [restored] = store.item_inventory(item_id)
    assert restored.size_class == "small"
    assert restored.size_source == "user"
    assert restored.size_confidence == 0.75
    assert restored.dimensions == {"length_mm": 100, "width_mm": 80}


def test_existing_items_receive_inferred_dimensions_on_database_open(tmp_path) -> None:
    path = tmp_path / "inventory.db"
    store = InventoryStore(path)
    item_id = store.upsert_item(name="temporary item")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE items SET name = ?, dimensions_json = NULL WHERE id = ?",
            ("M3 x 8 mm pan head screws", item_id),
        )

    reopened = InventoryStore(path)

    with reopened.connect() as conn:
        row = conn.execute(
            "SELECT dimensions_json FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    assert row is not None
    assert row["dimensions_json"] == '{"length_mm": 8.0, "thread_diameter_mm": 3.0}'


def test_locations_can_reference_photos_and_heic_extension_is_preserved(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    photo = store.add_photo(
        filename="bin-photo.HEIC",
        content_type="image/heic",
        data=b"heic-data",
    )

    store.attach_photo_to_location(location_id="printer-supplies-bin", photo_id=photo.id)

    [attached] = store.location_photos("printer-supplies-bin")
    assert attached.id == photo.id
    assert attached.location_id == "printer-supplies-bin"
    assert attached.content_type == "image/heic"
    assert attached.path.endswith(".heic")


def test_photo_target_can_move_between_item_and_location(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    photo = store.add_photo(
        filename="part.jpg",
        content_type="image/jpeg",
        data=b"photo",
    )
    item_id = store.upsert_item(name="solder wick")

    store.set_photo_target(photo_id=photo.id, item_id=item_id)
    item_photo = store.photo(photo.id)
    assert item_photo.item_id == item_id
    assert item_photo.location_id is None

    store.set_photo_target(photo_id=photo.id, location_id="B001")
    location_photo = store.photo(photo.id)
    assert location_photo.item_id is None
    assert location_photo.location_id == "B001"


def test_clear_bin_inventory_removes_only_that_bin(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    first = store.upsert_item(name="First item")
    second = store.upsert_item(name="Second item")
    store.put_inventory(item_id=first, bin_id="B001", quantity=1, unit="each")
    store.put_inventory(item_id=second, bin_id="B002", quantity=1, unit="each")

    removed = store.clear_bin_inventory("1")

    assert removed == 1
    assert store.bin_inventory("B001") == []
    assert store.bin_inventory("B002")[0].name == "Second item"


def test_inventory_helpers_preserve_dynamic_location_ids(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="Assorted brackets")

    store.put_inventory(
        item_id=item_id,
        bin_id="attic-parts-box",
        quantity=3,
        unit="each",
    )

    [stored] = store.bin_inventory("attic-parts-box")
    assert stored.bin_id == "attic-parts-box"
    assert store.move_inventory(
        item_id=item_id,
        from_bin="attic-parts-box",
        to_bin="garage-parts-box",
        normalize_bin=True,
    )
    [moved] = store.bin_inventory("garage-parts-box")
    assert moved.bin_id == "garage-parts-box"
    assert store.clear_bin_inventory("garage-parts-box") == 1
    assert store.list_inventory() == []


def test_remove_inventory_removes_only_requested_location(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="T8x8 lead screw nut")
    store.put_inventory(item_id=item_id, bin_id="B010", quantity=2, unit="each")
    store.put_inventory(
        item_id=item_id,
        bin_id="attic",
        quantity=2,
        unit="each",
        normalize_bin=False,
    )

    assert store.remove_inventory(item_id=item_id, bin_id="attic") is True

    remaining = store.list_inventory()
    assert [(item.name, item.bin_id) for item in remaining] == [
        ("T8x8 lead screw nut", "B010")
    ]
    assert store.remove_inventory(item_id=item_id, bin_id="attic") is False


def test_archived_item_is_hidden_without_losing_placements(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="Retired jig")
    store.put_inventory(item_id=item_id, bin_id="B010", quantity=1, unit="each")

    assert store.archive_item(item_id) is True
    assert store.list_inventory() == []
    assert store.search("Retired jig") == []
    assert store.occupied_bin_ids() == set()
    [archived] = store.list_inventory(include_archived=True)
    assert archived.active is False
    assert archived.archived_at
    assert archived.bin_id == "B010"

    assert store.restore_item(item_id) is True
    [restored] = store.list_inventory()
    assert restored.active is True
    assert restored.archived_at is None
    assert restored.bin_id == "B010"


def test_putting_away_archived_item_restores_it(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="Reusable fixture")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")
    assert store.archive_item(item_id) is True

    store.put_inventory(item_id=item_id, bin_id="B002", quantity=1, unit="each")

    assert {row.bin_id for row in store.list_inventory()} == {"B001", "B002"}


def test_item_merge_preserves_identity_and_combines_placements(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    source_photo = store.add_photo(
        filename="duplicate.jpg", content_type="image/jpeg", data=b"photo"
    )
    source_id = store.upsert_item(
        name="M4 tee nuts",
        description="Source description",
        aliases=["tee nuts"],
        photo_ids=[source_photo.id],
    )
    target_id = store.upsert_item(name="M4 T-nuts", aliases=["M4 hardware"])
    store.put_inventory(
        item_id=source_id, bin_id="B001", quantity=2, unit="each"
    )
    store.put_inventory(
        item_id=target_id, bin_id="B001", quantity=3, unit="each"
    )
    store.put_inventory(
        item_id=source_id,
        bin_id="attic-bin",
        quantity=4,
        unit="each",
        normalize_bin=False,
    )
    store.link_review_photo(
        item_id=source_id, bin_id="B001", photo_id=source_photo.id
    )

    preview = store.merge_item(
        source_item_id=source_id, target_item_id=target_id
    )

    assert preview["can_merge"] is True
    assert preview["photos_transferred"] == 1
    rows = store.item_inventory(target_id)
    assert [(row.bin_id, row.quantity) for row in rows] == [
        ("B001", 5),
        ("attic-bin", 4),
    ]
    assert {"M4 tee nuts", "tee nuts", "M4 hardware"} <= set(rows[0].aliases)
    assert rows[0].description == "Source description"
    assert rows[0].photos == [source_photo.url]
    assert store.resolve_item_id(source_id) == target_id
    assert store.restore_item(source_id) is False
    assert store.exact_match("M4 tee nuts")[0].item_id == target_id
    assert store.upsert_item(name="M4 tee nuts") == target_id


def test_item_merge_preview_blocks_conflicting_units(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    source_id = store.upsert_item(name="Loose washers")
    target_id = store.upsert_item(name="Washer packs")
    store.put_inventory(item_id=source_id, bin_id="B001", quantity=10, unit="each")
    store.put_inventory(item_id=target_id, bin_id="B001", quantity=1, unit="pack")

    preview = store.item_merge_preview(
        source_item_id=source_id, target_item_id=target_id
    )

    assert preview["can_merge"] is False
    assert preview["conflicts"] == [
        {
            "location_id": "B001",
            "source_unit": "each",
            "target_unit": "pack",
        }
    ]
    with pytest.raises(ValueError, match="different units"):
        store.merge_item(source_item_id=source_id, target_item_id=target_id)
    assert len(store.list_inventory()) == 2


def test_item_events_show_recent_inventory_history(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="T8x8 lead screw nut")
    store.put_inventory(item_id=item_id, bin_id="B010", quantity=2, unit="each")
    store.move_inventory(item_id=item_id, from_bin="B010", to_bin="B011")

    events = store.item_events(item_id)

    assert [event["event_type"] for event in events[:2]] == ["inventory_move", "inventory_put"]
    assert events[0]["bin_id"] == "B011"
    assert events[0]["quantity"] == 2
    assert events[0]["note"] == "from B010"
    assert events[0]["created_at"]


def test_items_track_home_and_last_seen_locations(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="M4 T-nuts")

    store.put_inventory(item_id=item_id, bin_id="B010", quantity=2, unit="each")

    [stored] = store.search("M4 T-nuts")
    assert stored.home_location_id == "B010"
    assert stored.last_seen_location_id == "B010"
    assert store.suggest_home("M4 T-nuts") == "B010"

    assert store.move_inventory(item_id=item_id, from_bin="B010", to_bin="B011") is True

    [moved] = store.search("M4 T-nuts")
    assert moved.home_location_id == "B010"
    assert moved.last_seen_location_id == "B011"
    assert store.suggest_home("M4 T-nuts") == "B010"


def test_confirmed_home_observations_adapt_and_respect_explicit_home(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="USB adapters")
    store.put_inventory(item_id=item_id, bin_id="B010", quantity=1, unit="each")

    first = store.observe_confirmed_home(item_id=item_id, location_id="B011")
    assert first["promoted"] is False
    assert first["matches"] == 1
    second = store.observe_confirmed_home(item_id=item_id, location_id="B011")
    assert second["promoted"] is True
    assert second["previous_home_location_id"] == "B010"
    assert store.suggest_home("USB adapters") == "B011"

    assert store.undo_confirmed_home_observation(int(second["home_observation"]))
    store.update_item_home(item_id=item_id, location_id="B010")
    locked = store.observe_confirmed_home(item_id=item_id, location_id="B011")
    assert locked["explicit_home"] is True
    assert locked["promoted"] is False
    assert store.suggest_home("USB adapters") == "B010"


def test_undone_home_observation_does_not_count_toward_learning(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="HDMI adapters")
    store.put_inventory(item_id=item_id, bin_id="B010", quantity=1, unit="each")

    first = store.observe_confirmed_home(item_id=item_id, location_id="B011")
    assert store.undo_confirmed_home_observation(int(first["home_observation"]))
    replacement = store.observe_confirmed_home(item_id=item_id, location_id="B011")

    assert replacement["matches"] == 1
    assert replacement["promoted"] is False
    assert store.suggest_home("HDMI adapters") == "B010"


def test_find_examples_prioritizes_photo_sort_feedback(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    store.record_example(
        command="sort this photo into separate bins",
        action='[{"tool":"put_item","arguments":{"item":"generic parts"}}]',
        kind="confirmed",
    )
    store.record_example(
        command="sort this photo into separate bins",
        action='{"rejected_plan":[{"tool":"put_item","arguments":{"item":"assorted hardware"}}]}',
        kind="rejected_photo_sort",
    )
    store.record_example(
        command="sort this photo into separate bins",
        action='{"corrected_plan":[{"tool":"put_item","arguments":{"item":"M4 T-nuts"}}]}',
        kind="edited_photo_sort",
    )
    store.record_example(
        command="sort this photo into separate bins review corrected generic nuts to M4 T-nuts",
        action='{"after":{"name":"M4 T-nuts"}}',
        kind="review_correction",
    )
    store.record_example(
        command="sort this photo into separate bins review approved M4 T-nuts in B010",
        action='{"approved_review":{"name":"M4 T-nuts","bin_id":"B010"}}',
        kind="review_approved",
    )

    kinds = [example["kind"] for example in store.find_examples("sort this photo", limit=5)]

    assert kinds == [
        "review_correction",
        "review_approved",
        "edited_photo_sort",
        "rejected_photo_sort",
        "confirmed",
    ]


def test_pending_assistant_confirmation_can_only_be_claimed_once(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    interaction_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="Put away washers",
        response={"plan": [{"tool": "put_item"}]},
        status="pending",
    )

    assert store.claim_assistant_interaction_confirmation(interaction_id) is True
    assert store.claim_assistant_interaction_confirmation(interaction_id) is False
    assert store.assistant_interaction(interaction_id)["status"] == "confirming"


def test_assistant_interactions_by_ids_is_bounded_deduplicated_and_ordered(
    tmp_path,
) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    first_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="First",
        response={"sequence": 1},
    )
    second_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="Second",
        response={"sequence": 2},
    )

    rows = store.assistant_interactions_by_ids(
        [second_id, first_id, second_id, 0, 9999]
    )

    assert [row["id"] for row in rows] == [first_id, second_id]
    assert [row["response"]["sequence"] for row in rows] == [1, 2]
    assert store.assistant_interactions_by_ids([]) == []


def test_active_guidance_state_round_trips(tmp_path) -> None:
    database_path = tmp_path / "inventory.db"
    store = InventoryStore(database_path)
    state = {
        "entries": [
            {
                "id": 4,
                "kind": "find",
                "query": "USB drive",
                "color": [255, 0, 0],
                "matches": [{"item_id": 7, "location_id": "B001"}],
            }
        ],
        "next_id": 5,
    }

    store.save_active_guidance_state(state)

    assert InventoryStore(database_path).load_active_guidance_state() == state


def test_new_agent_plan_supersedes_older_pending_plans(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    old_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="Put away old washers",
        response={"plan": [{"tool": "put_item"}]},
        status="pending",
    )

    assert store.supersede_pending_agent_plans() == 1
    new_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="Put away new washers",
        response={"plan": [{"tool": "put_item"}]},
        status="pending",
    )

    assert store.assistant_interaction(old_id)["status"] == "superseded"
    assert store.latest_pending_agent_plan()["id"] == new_id


def test_client_request_replay_lookup_is_exact_and_route_scoped(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    first_id = store.record_assistant_interaction(
        route="/assistant/chat",
        command="Find USB drives",
        response={"client_request_id": "request-123", "client_response": {"ok": True}},
    )
    store.record_assistant_interaction(
        route="/locations/B001/quick-put",
        command="USB drive",
        response={"client_request_id": "request-123", "client_response": {"ok": True}},
    )

    found = store.assistant_interaction_by_client_request_id(
        "request-123",
        route="/assistant/chat",
    )

    assert found is not None
    assert found["id"] == first_id
    assert store.assistant_interaction_by_client_request_id("missing") is None


def test_client_request_index_tolerates_legacy_invalid_response_json(tmp_path) -> None:
    database_path = tmp_path / "inventory.db"
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """
            CREATE TABLE assistant_interactions (
                id INTEGER PRIMARY KEY,
                route TEXT NOT NULL,
                command TEXT NOT NULL,
                photo_ids TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                response_json TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO assistant_interactions(route, command) VALUES (?, ?)",
            ("/assistant/chat", "legacy interaction"),
        )

    store = InventoryStore(database_path)

    assert store.assistant_interaction_by_client_request_id("missing") is None


def test_recent_voice_commands_count_one_completed_split_activation(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    store.record_voice_attempt(
        rms=900,
        activity_rms=1200,
        wake_word="magpie",
        wake_heard=True,
        awaiting_command=True,
        stt_model="local-vosk",
        audio_threshold=800,
    )
    store.record_voice_attempt(
        rms=100,
        activity_rms=120,
        wake_word="magpie",
        wake_heard=True,
        awaiting_command=True,
        stt_model="local-vosk",
        audio_threshold=800,
        error="awaiting_command_silence",
    )
    completed_id = store.record_voice_attempt(
        rms=1100,
        activity_rms=1800,
        transcript="Find USB dongles",
        wake_word="magpie",
        wake_heard=True,
        command="Find USB dongles",
        route="/assistant/chat",
        query="find USB dongles",
        stt_model="gpt-4o-transcribe",
        audio_threshold=800,
        device_id="main-cabinet",
        runtime_version="v0.1.0 abc1234",
        backend_response={"ok": True},
    )

    attempts = store.recent_voice_command_attempts(limit=5)

    assert [attempt["id"] for attempt in attempts] == [completed_id]
    assert attempts[0]["device_id"] == "main-cabinet"
    assert attempts[0]["runtime_version"] == "v0.1.0 abc1234"


def test_location_contents_page_counts_filters_and_bounds_rows(tmp_path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    for index in range(35):
        item_id = store.upsert_item(
            name=f"M4 washer {index:02d}",
            category="fasteners",
            aliases=[f"metric washer {index:02d}"],
        )
        store.put_inventory(
            item_id=item_id,
            bin_id="B001" if index < 20 else "B002",
            quantity=1,
            unit="each",
        )

    page, total = store.items_in_locations_page(
        ["B001", "B002"], limit=10, offset=20
    )
    filtered, filtered_total = store.items_in_locations_page(
        ["B001", "B002"], query="metric washer 3", limit=100
    )

    assert total == 35
    assert len(page) == 10
    assert filtered_total == 5
    assert len(filtered) == 5
    assert all("washer 3" in item.aliases[0] for item in filtered)


def test_inventory_catalog_page_bounds_groups_and_hydrates_every_placement(
    tmp_path,
) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    m2 = store.upsert_item(name="M2 bolts", category="hardware")
    store.put_inventory(item_id=m2, bin_id="B001", quantity=2, unit="each")
    store.put_inventory(item_id=m2, bin_id="B002", quantity=3, unit="each")
    m10 = store.upsert_item(name="M10 bolts", category="hardware")
    store.put_inventory(item_id=m10, bin_id="B001", quantity=10, unit="each")

    first = store.inventory_catalog_page(query="bolts", limit=1)
    second = store.inventory_catalog_page(query="bolts", limit=1, offset=1)

    assert first.total == 2
    assert [row.name for row in first.items] == ["M2 bolts", "M2 bolts"]
    assert {row.bin_id for row in first.items} == {"B001", "B002"}
    assert [row.name for row in second.items] == ["M10 bolts"]
    assert first.metrics == {
        "items": 2,
        "placements": 3,
        "locations": 2,
        "need_review": 0,
    }
    assert first.categories == ["hardware"]
    assert [
        row.item_id
        for row in store.inventory_catalog_page(sort="quantity").items
        if row.bin_id == "B001"
    ] == [m10, m2]
    assert store.inventory_catalog_page(sort="category").total == 2
    assert store.inventory_catalog_page(sort="location").total == 2
    scoped = store.inventory_catalog_page(location_ids={"B002"})
    assert scoped.total == 1
    assert [row.name for row in scoped.items] == ["M2 bolts", "M2 bolts"]
    assert {row.bin_id for row in scoped.items} == {"B001", "B002"}
    assert scoped.metrics == {
        "items": 1,
        "placements": 1,
        "locations": 1,
        "need_review": 0,
    }
