import json
from pathlib import Path

from fastapi.testclient import TestClient

from searchable_storage_led.api import create_app
from searchable_storage_led.config import LedConfig, ZebraConfig
from searchable_storage_led.store import InventoryStore


ROOT = Path(__file__).resolve().parents[1]


def product_client(tmp_path, *, zebra: ZebraConfig | None = None) -> TestClient:
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
    return TestClient(
        create_app(
            LedConfig(
                pixel_count=1,
                backend="none",
                layout_path=str(layout_path),
                database_path=str(tmp_path / "inventory.db"),
                zebra=zebra or ZebraConfig(),
            )
        )
    )


def test_repeated_location_names_at_one_level_are_rejected(tmp_path) -> None:
    client = product_client(tmp_path)

    first_response = client.post(
        "/locations",
        json={"type": "cabinet", "label": "Printer Cabinet"},
    )
    second_response = client.post(
        "/locations",
        json={"type": "cabinet", "label": " printer   cabinet "},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 409
    assert "already exists at the top level" in second_response.json()["detail"]
    first = first_response.json()
    assert first["id"] == "printer-cabinet"
    assert first["location"]["parent_id"] is None
    assert client.get("/locations/printer-cabinet").json()["path"] == [
        {
            "id": "printer-cabinet",
            "label": "Printer Cabinet",
        }
    ]
    assert client.get("/locations").json()["roots"] == ["printer-cabinet"]


def test_repeated_names_in_different_branches_keep_clean_path_labels(tmp_path) -> None:
    client = product_client(tmp_path)
    garage = client.post(
        "/locations", json={"type": "room", "label": "Garage"}
    ).json()["id"]
    attic = client.post(
        "/locations", json={"type": "room", "label": "Attic"}
    ).json()["id"]
    garage_drawer = client.post(
        "/locations",
        json={"type": "drawer", "label": "Drawer 1", "parent_id": garage},
    ).json()["id"]
    attic_drawer = client.post(
        "/locations",
        json={"type": "drawer", "label": "Drawer 1", "parent_id": attic},
    ).json()["id"]

    assert client.get(f"/locations/{garage_drawer}").json()["path"] == [
        {"id": garage, "label": "Garage"},
        {"id": garage_drawer, "label": "Drawer 1"},
    ]
    assert client.get(f"/locations/{attic_drawer}").json()["path"] == [
        {"id": attic, "label": "Attic"},
        {"id": attic_drawer, "label": "Drawer 1"},
    ]


def test_scanned_location_separates_quick_add_from_magpie_putaway(tmp_path) -> None:
    client = product_client(tmp_path)
    location_id = client.post(
        "/locations",
        json={"type": "bin", "label": "Hardware Bin"},
    ).json()["id"]

    response = client.get(f"/l/{location_id}")

    assert response.status_code == 200
    html = response.text
    assert 'id="quickPutAction" href="#quickPut">Quick add here</a>' in html
    assert "Put away with Magpie</a>" in html
    assert f"location={location_id}" in html
    assert "action=put-away" in html
    assert "Manage place</a>" in html
    assert f"Quick add to Hardware Bin</label>" in html
    assert '<button id="quickSubmit" type="submit">Save here</button>' in html
    assert 'document.getElementById("quickText").focus()' in html
    assert "Download label</a>" in html
    assert 'id="light" type="button"' not in html
    assert 'id="print" type="button"' not in html
    assert "if (lightButton)" in html
    assert "if (printButton)" in html
    assert "Raw API" not in html


def test_scanned_location_shows_configured_light_and_printer_actions(tmp_path) -> None:
    client = product_client(
        tmp_path,
        zebra=ZebraConfig(host="printer.local"),
    )
    location_id = client.post(
        "/locations",
        json={"type": "bin", "label": "Lit Hardware Bin"},
    ).json()["id"]
    configured = client.post(
        f"/locations/{location_id}/update",
        json={"light_target": "main", "light_indexes": [0]},
    )
    assert configured.status_code == 200

    response = client.get(f"/l/{location_id}")

    assert response.status_code == 200
    assert 'id="light" type="button">Light spot</button>' in response.text
    assert 'id="print" type="button">Print label</button>' in response.text
    assert "Download label</a>" not in response.text


def test_location_item_destination_defaults_can_be_changed_and_undone(tmp_path) -> None:
    client = product_client(tmp_path)

    room = client.post(
        "/locations",
        json={"type": "room", "label": "Workshop"},
    ).json()["location"]
    cabinet = client.post(
        "/locations",
        json={"type": "cabinet", "label": "Tool Cabinet", "parent_id": room["id"]},
    ).json()["location"]
    drawer = client.post(
        "/locations",
        json={"type": "drawer", "label": "Drawer 1", "parent_id": cabinet["id"]},
    ).json()["location"]
    direct_cabinet = client.post(
        "/locations",
        json={
            "type": "cabinet",
            "label": "Open Cabinet",
            "parent_id": room["id"],
            "accepts_items": True,
        },
    ).json()["location"]

    assert room["accepts_items"] is False
    assert cabinet["accepts_items"] is False
    assert drawer["accepts_items"] is True
    assert direct_cabinet["accepts_items"] is True
    rejected = client.post(
        f"/locations/{cabinet['id']}/items",
        json={"name": "Loose wrench"},
    )
    assert rejected.status_code == 400
    assert "does not hold items directly" in rejected.json()["detail"]

    changed = client.post(
        f"/locations/{drawer['id']}/update",
        json={"accepts_items": False},
    ).json()
    assert changed["applied"][0]["previous_accepts_items"] is True
    assert client.get(f"/locations/{drawer['id']}").json()["location"][
        "accepts_items"
    ] is False

    assert client.post("/assistant/undo").json()["undone"] == [
        {"restored_location": drawer["id"]}
    ]
    assert client.get(f"/locations/{drawer['id']}").json()["location"][
        "accepts_items"
    ] is True


def test_occupied_location_cannot_be_changed_to_organizer_only(tmp_path) -> None:
    client = product_client(tmp_path)
    drawer = client.post(
        "/locations",
        json={"type": "drawer", "label": "Hardware Drawer"},
    ).json()["location"]
    client.post(
        f"/locations/{drawer['id']}/items",
        json={"name": "M4 T-nuts"},
    )

    rejected = client.post(
        f"/locations/{drawer['id']}/update",
        json={"accepts_items": False},
    )

    assert rejected.status_code == 400
    assert "move this place's direct items" in rejected.json()["detail"]
    assert client.get(f"/locations/{drawer['id']}").json()["location"][
        "accepts_items"
    ] is True


def test_put_away_into_organizer_selects_eligible_descendant(tmp_path) -> None:
    client = product_client(tmp_path)
    cabinet = client.post(
        "/locations",
        json={"type": "cabinet", "label": "Camera Cabinet"},
    ).json()["location"]
    drawer = client.post(
        "/locations",
        json={
            "type": "drawer",
            "label": "General Drawer",
            "parent_id": cabinet["id"],
            "capacity_class": "small",
        },
    ).json()["location"]

    response = client.post(
        "/assistant/chat",
        json={"text": "Put away camera cable in Camera Cabinet", "mode": "auto"},
    ).json()

    assert response["decision"]["action"] == "put_away"
    assert response["decision"]["destination"]["id"] == drawer["id"]
    assert "inside Camera Cabinet" in response["decision"]["placement"]["reasons"]


def test_scoped_putaway_handoff_selects_an_eligible_descendant(tmp_path) -> None:
    client = product_client(tmp_path)
    cabinet = client.post(
        "/locations",
        json={"type": "cabinet", "label": "Network Cabinet"},
    ).json()["location"]
    drawer = client.post(
        "/locations",
        json={
            "type": "drawer",
            "label": "Drawer 1",
            "parent_id": cabinet["id"],
            "capacity_class": "small",
        },
    ).json()["location"]
    client.post(
        "/locations",
        json={
            "type": "bin",
            "label": "Ethernet adapters",
            "capacity_class": "small",
        },
    )

    response = client.post(
        "/assistant/chat",
        json={
            "text": "Put away Ethernet adapter",
            "current_location_id": cabinet["id"],
            "source": "mobile",
        },
    ).json()

    assert response["decision"]["action"] == "put_away"
    assert response["decision"]["destination"]["id"] == drawer["id"]
    assert "inside Network Cabinet" in response["decision"]["placement"]["reasons"]


def test_dynamic_location_review_can_be_approved_and_undone(tmp_path) -> None:
    client = product_client(tmp_path)
    location_id = client.post(
        "/locations",
        json={"type": "box", "label": "Attic Parts Box"},
    ).json()["id"]
    created = client.post(
        "/inventory",
        json={"name": "Unsorted brackets", "bin_id": location_id},
    ).json()
    store = InventoryStore(tmp_path / "inventory.db")
    store.mark_needs_review(
        item_id=created["item_id"],
        bin_id=location_id,
        reason="Confirm bracket type.",
    )

    approved = client.post(
        "/inventory/reviewed",
        json={"item_id": created["item_id"], "bin_id": location_id},
    )

    assert approved.status_code == 200
    assert approved.json()["bin_id"] == location_id
    assert approved.json()["interaction_id"] > 0
    assert approved.json()["applied"] == [
        {
            "approved_review": created["item_id"],
            "item": "Unsorted brackets",
            "location_id": location_id,
            "previous_review_reason": "Confirm bracket type.",
            "review_example_id": approved.json()["applied"][0]["review_example_id"],
        }
    ]
    review_example_id = approved.json()["applied"][0]["review_example_id"]
    assert review_example_id > 0
    assert store.example(review_example_id)["review_status"] == "approved"
    assert client.get("/inventory/review").json()["items"] == []

    undone = client.post(
        "/assistant/undo",
        json={"expected_interaction_id": approved.json()["interaction_id"]},
    ).json()

    assert undone["ok"] is True
    [restored] = client.get("/inventory/review").json()["items"]
    assert restored["bin_id"] == location_id
    assert restored["location_path"] == [
        {"id": location_id, "label": "Attic Parts Box"}
    ]
    assert restored["location_path_label"] == "Attic Parts Box"
    assert restored["review_reason"] == "Confirm bracket type."
    assert store.example(review_example_id)["review_status"] == "rejected"


def test_uncertain_placement_removal_is_explained_and_undo_restores_review(
    tmp_path,
) -> None:
    client = product_client(tmp_path)
    location_id = client.post(
        "/locations",
        json={"type": "drawer", "label": "Fastener drawer"},
    ).json()["id"]
    created = client.post(
        "/inventory",
        json={"name": "M3 flush screws", "bin_id": location_id, "quantity": 40},
    ).json()
    store = InventoryStore(tmp_path / "inventory.db")
    store.mark_needs_review(
        item_id=created["item_id"],
        bin_id=location_id,
        reason="Reported missing during retrieval.",
    )

    sole_removal = client.delete(
        f"/inventory/{created['item_id']}/locations/{location_id}"
    )
    assert sole_removal.status_code == 409
    assert sole_removal.json()["detail"] == (
        "this is the item's only recorded placement; archive the item instead of "
        "leaving an active item without a location"
    )

    other_location_id = client.post(
        "/locations",
        json={"type": "bin", "label": "Overflow fasteners"},
    ).json()["id"]
    duplicate = client.post(
        f"/locations/{other_location_id}/items",
        json={"name": "M3 flush screws", "quantity": 5},
    ).json()
    assert duplicate["item_id"] == created["item_id"]

    removed = client.delete(
        f"/inventory/{created['item_id']}/locations/{location_id}"
    )

    assert removed.status_code == 200
    body = removed.json()
    assert body["resolved_review_count"] == 1
    assert body["applied"][0]["review_resolution"] == "removed_missing_placement"
    assert body["applied"][0]["previous_needs_review"] is True
    assert body["applied"][0]["previous_review_reason"] == (
        "Reported missing during retrieval."
    )
    assert client.get("/inventory/review").json()["items"] == []
    history = client.get(f"/items/{created['item_id']}/history").json()["events"][0]
    assert history["event_type"] == "inventory_remove"
    assert history["note"] == (
        "Review resolved: recorded placement removed after the item could not be found."
    )

    undone = client.post(
        "/assistant/undo",
        json={"expected_interaction_id": body["interaction_id"]},
    )

    assert undone.status_code == 200
    [restored] = client.get("/inventory/review").json()["items"]
    assert restored["item_id"] == created["item_id"]
    assert restored["bin_id"] == location_id
    assert restored["quantity"] == 40
    assert restored["review_reason"] == "Reported missing during retrieval."


def test_review_queue_endpoint_returns_bounded_decision_categories(tmp_path) -> None:
    client = product_client(tmp_path)
    first_location = client.post(
        "/locations",
        json={"type": "box", "label": "First Review Box"},
    ).json()["id"]
    second_location = client.post(
        "/locations",
        json={"type": "box", "label": "Second Review Box"},
    ).json()["id"]
    created = client.post(
        "/inventory",
        json={"name": "Uncertain fastener", "bin_id": first_location},
    ).json()
    client.post(
        "/inventory",
        json={"name": "Known washer", "bin_id": second_location},
    )
    store = InventoryStore(tmp_path / "inventory.db")
    store.mark_needs_review(
        item_id=created["item_id"],
        bin_id=first_location,
        reason="Confirm fastener type.",
    )
    store.record_example(
        command="Put away the uncertain fastener",
        action='[{"tool":"put_item","arguments":{"item":"fastener"}}]',
        kind="correction",
    )
    store.learn_rule(
        rule_text="Fasteners go in the first box",
        trigger_terms=["fasteners"],
        target_location_id=first_location,
    )
    store.learn_rule(
        rule_text="Fasteners go in the second box",
        trigger_terms=["fasteners"],
        target_location_id=second_location,
    )

    response = client.get("/review/queue?limit=1")

    assert response.status_code == 200
    queue = response.json()
    assert queue["limit"] == 1
    assert [item["name"] for item in queue["items"]] == ["Uncertain fastener"]
    assert [example["command"] for example in queue["learning_examples"]] == [
        "Put away the uncertain fastener"
    ]
    assert queue["rule_issues"][0]["title"] == "Conflicting placement rules"
    assert [place["location_id"] for place in queue["place_checks"]] == [
        first_location
    ]
    assert queue["place_checks"][0]["confidence"]["state"] == "attention"
    assert queue["has_more"] == {
        "items": False,
        "learning_examples": True,
        "rule_issues": False,
        "place_checks": True,
    }


def test_parent_contents_check_resolves_nested_place_review_work(tmp_path) -> None:
    client = product_client(tmp_path)
    root = client.post(
        "/locations",
        json={"type": "cabinet", "label": "Workshop Cabinet"},
    ).json()["id"]
    child = client.post(
        "/locations",
        json={"type": "drawer", "label": "Hardware", "parent_id": root},
    ).json()["id"]
    stored = client.post(
        "/inventory",
        json={"name": "M4 washers", "bin_id": child, "quantity": 20},
    ).json()

    before = client.get("/review/queue").json()
    assert [place["location_id"] for place in before["place_checks"]] == [child]
    assert before["place_checks"][0]["confidence"]["state"] == "unchecked"

    audit = client.get(f"/locations/{root}/audit").json()
    checked = client.post(
        f"/locations/{root}/verify",
        json={
            "revision": audit["revision"],
            "placements": [
                {
                    "item_id": stored["item_id"],
                    "location_id": child,
                    "status": "present",
                }
            ],
        },
    )
    assert checked.status_code == 200

    after = client.get("/review/queue").json()
    assert after["place_checks"] == []
    child_detail = client.get(f"/locations/{child}").json()
    assert child_detail["confidence"]["state"] == "checked"
    assert child_detail["verification"]["root_location_id"] == root
    assert child_detail["verification"]["placement_count"] == 1


def test_inventory_changes_make_verified_places_due_again(tmp_path) -> None:
    client = product_client(tmp_path)
    root = client.post(
        "/locations",
        json={"type": "cabinet", "label": "Main Cabinet"},
    ).json()["id"]
    child = client.post(
        "/locations",
        json={"type": "drawer", "label": "Hardware", "parent_id": root},
    ).json()["id"]
    stored = client.post(
        "/inventory",
        json={"name": "M4 washers", "bin_id": child, "quantity": 20},
    ).json()
    audit = client.get(f"/locations/{root}/audit").json()
    assert client.post(
        f"/locations/{root}/verify",
        json={
            "revision": audit["revision"],
            "placements": [
                {
                    "item_id": stored["item_id"],
                    "location_id": child,
                    "status": "present",
                }
            ],
        },
    ).status_code == 200
    assert client.get(f"/locations/{root}").json()["confidence"]["state"] == "checked"
    checked_readiness = client.get("/product/readiness").json()
    checked_integrity = next(
        check
        for check in checked_readiness["checks"]
        if check["key"] == "physical_storage_integrity"
    )
    assert checked_integrity["passed"] is True
    assert checked_readiness["physical_storage_checks"] == {
        "due": 0,
        "has_more": False,
        "states": {},
    }

    updated = client.post(
        f"/inventory/{stored['item_id']}",
        json={
            "name": "M4 washers",
            "bin_id": child,
            "quantity": 24,
            "unit": "each",
        },
    )
    assert updated.status_code == 200

    root_detail = client.get(f"/locations/{root}").json()
    child_detail = client.get(f"/locations/{child}").json()
    assert root_detail["confidence"]["state"] == "changed"
    assert child_detail["confidence"]["state"] == "changed"
    assert root_detail["confidence"]["label"] == "Contents changed since last check"
    queue = client.get("/review/queue").json()
    [due] = queue["place_checks"]
    assert due["location_id"] == child
    assert due["confidence"]["state"] == "changed"
    assert due["action_label"] == "Recheck"
    changed_readiness = client.get("/product/readiness").json()
    changed_integrity = next(
        check
        for check in changed_readiness["checks"]
        if check["key"] == "physical_storage_integrity"
    )
    assert changed_integrity["passed"] is False
    assert changed_integrity["action"] == {
        "label": "Review storage checks",
        "href": "/review",
    }
    assert changed_readiness["physical_storage_checks"] == {
        "due": 1,
        "has_more": False,
        "states": {"changed": 1},
    }

    recheck = client.get(f"/locations/{root}/audit").json()
    assert client.post(
        f"/locations/{root}/verify",
        json={
            "revision": recheck["revision"],
            "placements": [
                {
                    "item_id": stored["item_id"],
                    "location_id": child,
                    "status": "present",
                }
            ],
        },
    ).status_code == 200

    assert client.get("/review/queue").json()["place_checks"] == []
    for location_id in (root, child):
        detail = client.get(f"/locations/{location_id}").json()
        assert detail["confidence"]["state"] == "checked"
        assert (
            detail["verification"]["content_revision"]
            == detail["confidence"]["content_revision"]
        )
    rechecked_readiness = client.get("/product/readiness").json()
    rechecked_integrity = next(
        check
        for check in rechecked_readiness["checks"]
        if check["key"] == "physical_storage_integrity"
    )
    assert rechecked_integrity["passed"] is True
    assert rechecked_readiness["physical_storage_checks"]["due"] == 0


def test_stale_review_receipt_cannot_undo_a_newer_storage_change(tmp_path) -> None:
    client = product_client(tmp_path)
    location_id = client.post(
        "/locations",
        json={"type": "box", "label": "Review Box"},
    ).json()["id"]
    created = client.post(
        "/inventory",
        json={"name": "Unsorted clips", "bin_id": location_id},
    ).json()
    store = InventoryStore(tmp_path / "inventory.db")
    store.mark_needs_review(
        item_id=created["item_id"],
        bin_id=location_id,
        reason="Confirm clip type.",
    )
    approved = client.post(
        "/inventory/reviewed",
        json={"item_id": created["item_id"], "bin_id": location_id},
    ).json()
    newer = client.post(
        "/locations",
        json={"type": "box", "label": "Newer Box"},
    )

    stale_undo = client.post(
        "/assistant/undo",
        json={"expected_interaction_id": approved["interaction_id"]},
    )

    assert newer.status_code == 200
    assert stale_undo.status_code == 409
    assert stale_undo.json()["detail"] == {
        "message": (
            "A newer storage change happened after this receipt. "
            "Refresh before choosing what to undo."
        ),
        "expected_interaction_id": approved["interaction_id"],
        "latest_interaction_id": newer.json()["interaction_id"],
    }
    assert client.get("/inventory/review").json()["items"] == []
    assert client.get("/locations/newer-box").status_code == 200


def test_batch_create_adds_uniquely_named_children(tmp_path) -> None:
    client = product_client(tmp_path)
    root = client.post(
        "/locations",
        json={"type": "room", "label": "Attic"},
    ).json()

    response = client.post(
        f"/locations/{root['id']}/children/batch",
        json={
            "count": 3,
            "type": "tote",
            "label_prefix": "Blue Tote",
            "capacity_class": "large",
            "internal_dimensions": {
                "width_mm": 500,
                "depth_mm": 700,
                "height_mm": 400,
            },
        },
    )

    assert response.status_code == 200
    created = response.json()["created"]
    assert [row["label"] for row in created] == [
        "Blue Tote 1",
        "Blue Tote 2",
        "Blue Tote 3",
    ]
    assert all(row["parent_id"] == "attic" for row in created)
    assert all(row["capacity_class"] == "large" for row in created)
    assert all(
        row["internal_dimensions"]
        == {"width_mm": 500.0, "depth_mm": 700.0, "height_mm": 400.0}
        for row in created
    )


def test_manual_location_create_and_batch_create_are_undoable(tmp_path) -> None:
    client = product_client(tmp_path)

    created = client.post(
        "/locations",
        json={"type": "room", "label": "Workshop"},
    ).json()

    assert created["action"] == "manual_create_location"
    assert created["interaction_id"] > 0
    assert created["applied"] == [{"created_location": "workshop"}]
    create_undo = client.post("/assistant/undo").json()
    assert create_undo["undone"] == [
        {"deleted_location": "workshop", "removed": ["workshop"]}
    ]
    assert client.get("/locations/workshop").status_code == 404

    parent_id = client.post(
        "/locations", json={"type": "room", "label": "Garage"}
    ).json()["id"]
    batch = client.post(
        f"/locations/{parent_id}/children/batch",
        json={"count": 3, "type": "shelf", "label_prefix": "Wall Shelf"},
    ).json()

    assert batch["action"] == "manual_create_location_batch"
    assert batch["applied"] == [
        {"created_location": "garage-wall-shelf-1"},
        {"created_location": "garage-wall-shelf-2"},
        {"created_location": "garage-wall-shelf-3"},
    ]
    batch_undo = client.post("/assistant/undo").json()
    assert [row["deleted_location"] for row in batch_undo["undone"]] == [
        "garage-wall-shelf-3",
        "garage-wall-shelf-2",
        "garage-wall-shelf-1",
    ]
    assert client.get(f"/locations/{parent_id}").status_code == 200
    assert client.get(f"/locations/{parent_id}/tree").json()["children"] == []


def test_named_location_batch_builds_distinct_children_as_one_undoable_action(
    tmp_path,
) -> None:
    client = product_client(tmp_path)
    home_id = client.post(
        "/locations", json={"type": "home", "label": "My Home"}
    ).json()["id"]

    response = client.post(
        f"/locations/{home_id}/children/batch",
        json={
            "labels": ["Garage", "  Attic storage  ", "Office"],
            "type": "room",
            "capacity_class": "large",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert [row["label"] for row in body["created"]] == [
        "Garage",
        "Attic storage",
        "Office",
    ]
    assert all(row["parent_id"] == home_id for row in body["created"])
    assert all(row["type"] == "room" for row in body["created"])
    assert body["action"] == "manual_create_location_batch"

    duplicate = client.post(
        f"/locations/{home_id}/children/batch",
        json={"labels": ["Workshop", " workshop "], "type": "room"},
    )
    assert duplicate.status_code == 400
    assert duplicate.json()["detail"] == "place names in one batch must be unique"

    undone = client.post("/assistant/undo").json()
    assert [row["deleted_location"] for row in undone["undone"]] == [
        "my-home-office",
        "my-home-attic-storage",
        "my-home-garage",
    ]


def test_structure_template_builds_nested_storage_as_one_undoable_action(
    tmp_path,
) -> None:
    client = product_client(tmp_path)
    garage_id = client.post(
        "/locations",
        json={"type": "room", "label": "Garage"},
    ).json()["id"]

    response = client.post(
        "/locations/structure",
        json={
            "label": "Fastener Cabinet",
            "type": "cabinet",
            "parent_id": garage_id,
            "capacity_class": "large",
            "child_count": 4,
            "child_type": "drawer",
            "child_label_prefix": "Drawer",
            "child_capacity_class": "small",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["structure"]["id"] == "garage-fastener-cabinet"
    assert body["structure"]["parent_id"] == garage_id
    assert body["structure"]["type"] == "cabinet"
    assert body["structure"]["capacity_class"] == "large"
    assert [child["label"] for child in body["children"]] == [
        "Drawer 1",
        "Drawer 2",
        "Drawer 3",
        "Drawer 4",
    ]
    assert all(
        child["parent_id"] == "garage-fastener-cabinet"
        and child["type"] == "drawer"
        and child["capacity_class"] == "small"
        for child in body["children"]
    )
    assert body["action"] == "manual_create_location_structure"
    assert body["applied"] == [
        {"created_location": "garage-fastener-cabinet"},
        {"created_location": "garage-fastener-cabinet-drawer-1"},
        {"created_location": "garage-fastener-cabinet-drawer-2"},
        {"created_location": "garage-fastener-cabinet-drawer-3"},
        {"created_location": "garage-fastener-cabinet-drawer-4"},
    ]

    tree = client.get("/locations/garage-fastener-cabinet/tree").json()
    assert [child["label"] for child in tree["children"]] == [
        "Drawer 1",
        "Drawer 2",
        "Drawer 3",
        "Drawer 4",
    ]

    undone = client.post("/assistant/undo").json()
    assert [row["deleted_location"] for row in undone["undone"]] == [
        "garage-fastener-cabinet-drawer-4",
        "garage-fastener-cabinet-drawer-3",
        "garage-fastener-cabinet-drawer-2",
        "garage-fastener-cabinet-drawer-1",
        "garage-fastener-cabinet",
    ]
    assert client.get("/locations/garage-fastener-cabinet").status_code == 404
    assert client.get(f"/locations/{garage_id}").status_code == 200


def test_common_household_container_types_are_supported_consistently(tmp_path) -> None:
    client = product_client(tmp_path)

    setup = client.post(
        "/storage/setup",
        json={
            "root_name": "Hall Closet",
            "root_type": "closet",
            "children": [
                {"label": "Hardware Tray", "type": "tray"},
                {"label": "Winter Bag", "type": "bag"},
            ],
            "destination": {
                "label": "Parts Organizer",
                "type": "organizer",
                "capacity_class": "small",
            },
        },
    )

    assert setup.status_code == 200
    created = setup.json()["created"]
    by_type = {location["type"]: location for location in created}
    assert by_type["closet"]["accepts_items"] is False
    for location_type in ("tray", "bag", "organizer"):
        assert by_type[location_type]["accepts_items"] is True

    root_id = setup.json()["root"]["id"]
    invalid_create = client.post(
        "/locations",
        json={"label": "Invalid", "type": "spaceship", "parent_id": root_id},
    )
    invalid_batch = client.post(
        f"/locations/{root_id}/children/batch",
        json={"labels": ["Invalid"], "type": "spaceship"},
    )
    invalid_fill = client.post(
        f"/locations/{root_id}/fill",
        json={"cols": 1, "rows": 1, "cell_type": "spaceship"},
    )
    invalid_cell = client.post(
        f"/locations/{root_id}/cell",
        json={"col": 1, "row": 1, "cell_type": "spaceship"},
    )
    invalid_update = client.post(
        f"/locations/{root_id}/update",
        json={"type": "spaceship"},
    )
    for response in (
        invalid_create,
        invalid_batch,
        invalid_fill,
        invalid_cell,
        invalid_update,
    ):
        assert response.status_code == 400
        assert response.json()["detail"] == "unknown storage place type"


def test_structure_template_validates_before_writing(tmp_path) -> None:
    client = product_client(tmp_path)

    unknown_parent = client.post(
        "/locations/structure",
        json={
            "label": "Cabinet",
            "type": "cabinet",
            "parent_id": "missing",
            "child_count": 4,
            "child_type": "drawer",
            "child_label_prefix": "Drawer",
        },
    )
    assert unknown_parent.status_code == 400
    assert unknown_parent.json()["detail"] == "unknown parent location"

    unknown_type = client.post(
        "/locations/structure",
        json={
            "label": "Cabinet",
            "type": "spaceship",
            "child_count": 4,
            "child_type": "drawer",
            "child_label_prefix": "Drawer",
        },
    )
    assert unknown_type.status_code == 400
    assert unknown_type.json()["detail"] == "unknown storage place type"

    long_child_name = client.post(
        "/locations/structure",
        json={
            "label": "Cabinet",
            "type": "cabinet",
            "child_count": 100,
            "child_type": "drawer",
            "child_label_prefix": "D" * 117,
        },
    )
    assert long_child_name.status_code == 400
    assert (
        long_child_name.json()["detail"]
        == "inside place names must be 120 characters or fewer"
    )
    assert client.get("/locations").json() == {"roots": [], "locations": []}


def test_location_ids_do_not_overwrite_archived_places(tmp_path) -> None:
    client = product_client(tmp_path)
    original = client.post(
        "/locations",
        json={"type": "cabinet", "label": "Printer Cabinet"},
    ).json()["id"]
    assert client.post(f"/locations/{original}/archive").status_code == 200

    replacement = client.post(
        "/locations",
        json={"type": "cabinet", "label": "Printer Cabinet"},
    ).json()

    assert replacement["id"] == "printer-cabinet-2"
    archived = client.get("/locations?include_archived=true").json()["locations"]
    assert {location["id"] for location in archived} == {
        "printer-cabinet",
        "printer-cabinet-2",
    }
    assert next(
        location for location in archived if location["id"] == "printer-cabinet"
    )["active"] is False


def test_rules_management_lists_updates_disables_and_restores_rules(tmp_path) -> None:
    client = product_client(tmp_path)
    cabinet_id = client.post(
        "/locations",
        json={"type": "cabinet", "label": "Main Cabinet"},
    ).json()["id"]
    placement = client.post(
        "/assistant/confirm",
        json={
            "command": "I always keep jewelry in the main cabinet",
            "plan": [
                {
                    "tool": "learn_rule",
                    "arguments": {
                        "rule_text": "Jewelry belongs in the main cabinet.",
                        "trigger_terms": ["jewelry"],
                        "target_location_id": cabinet_id,
                    },
                }
            ],
        },
    ).json()
    placement_id = placement["applied"][0]["learned_rule"]
    size = client.post(
        "/assistant/confirm",
        json={
            "command": "Jewelry boxes need medium space",
            "plan": [
                {
                    "tool": "learn_size_rule",
                    "arguments": {
                        "rule_text": "Jewelry boxes need medium space.",
                        "trigger_terms": ["jewelry", "box"],
                        "size_class": "medium",
                    },
                }
            ],
        },
    ).json()
    size_id = size["applied"][0]["learned_size_rule"]

    listed = client.get("/rules").json()
    placement_rule = next(
        rule for rule in listed["placement_rules"] if rule["id"] == placement_id
    )
    assert placement_rule["target_path_label"] == "Main Cabinet"
    assert placement_rule["target_active"] is True
    assert next(rule for rule in listed["size_rules"] if rule["id"] == size_id)[
        "size_class"
    ] == "medium"

    updated = client.patch(
        f"/rules/size/{size_id}",
        json={
            "rule_text": "Jewelry boxes need large space.",
            "trigger_terms": ["jewelry box"],
            "size_class": "large",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["action"] == "manual_update_rule"
    assert client.get("/rules").json()["size_rules"][0]["size_class"] == "large"
    assert client.post("/assistant/undo").json()["undone"] == [
        {"restored_rule": size_id, "rule_type": "size"}
    ]
    assert client.get("/rules").json()["size_rules"][0]["size_class"] == "medium"

    disabled = client.post(f"/rules/placement/{placement_id}/disable")
    assert disabled.status_code == 200
    assert next(
        rule
        for rule in client.get("/rules").json()["placement_rules"]
        if rule["id"] == placement_id
    )["active"] is False
    assert client.post("/assistant/undo").json()["undone"] == [
        {"restored_rule": placement_id, "rule_type": "placement"}
    ]
    assert client.post(f"/rules/placement/{placement_id}/disable").status_code == 200
    assert client.post(f"/rules/placement/{placement_id}/restore").status_code == 200


def test_rules_management_rejects_invalid_rule_changes(tmp_path) -> None:
    client = product_client(tmp_path)

    assert client.patch(
        "/rules/placement/999",
        json={
            "rule_text": "Missing rule",
            "trigger_terms": ["missing"],
            "target_location_id": "missing",
        },
    ).status_code == 404
    assert client.post("/rules/unknown/1/disable").status_code == 400
    assert client.post("/rules/size/1/delete").status_code == 400


def test_guided_storage_setup_creates_one_undoable_hierarchy(tmp_path) -> None:
    client = product_client(tmp_path)

    response = client.post(
        "/storage/setup",
        json={
            "root_name": "  My   Home  ",
            "root_type": "home",
            "children": [
                {"label": "Garage", "type": "room"},
                {"label": "Attic", "type": "room"},
                {"label": "Utility room", "type": "room"},
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["root"]["id"] == "my-home"
    assert body["root"]["label"] == "My Home"
    assert [child["label"] for child in body["children"]] == [
        "Garage",
        "Attic",
        "Utility room",
    ]
    assert all(child["parent_id"] == "my-home" for child in body["children"])
    assert body["action"] == "manual_storage_setup"
    assert body["applied"] == [
        {"created_location": "my-home"},
        {"created_location": "my-home-garage"},
        {"created_location": "my-home-attic"},
        {"created_location": "my-home-utility-room"},
    ]
    assert [location["id"] for location in body["created"]] == [
        "my-home",
        "my-home-garage",
        "my-home-attic",
        "my-home-utility-room",
    ]

    tree = client.get("/locations").json()
    assert tree["roots"] == ["my-home"]
    assert len(tree["locations"]) == 4

    undone = client.post("/assistant/undo").json()
    assert [entry["deleted_location"] for entry in undone["undone"]] == [
        "my-home-utility-room",
        "my-home-attic",
        "my-home-garage",
        "my-home",
    ]
    assert client.get("/locations").json() == {"roots": [], "locations": []}


def test_guided_storage_setup_creates_a_ready_first_destination(tmp_path) -> None:
    client = product_client(tmp_path)

    response = client.post(
        "/storage/setup",
        json={
            "root_name": "My Home",
            "root_type": "home",
            "children": [
                {"label": "Garage", "type": "room"},
                {"label": "Attic", "type": "room"},
            ],
            "destination": {
                "label": "Hardware bin",
                "type": "bin",
                "parent_label": " garage ",
                "capacity_class": "small",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert [child["label"] for child in body["children"]] == ["Garage", "Attic"]
    assert body["destination"]["id"] == "my-home-garage-hardware-bin"
    assert body["destination"]["label"] == "Hardware bin"
    assert body["destination"]["type"] == "bin"
    assert body["destination"]["parent_id"] == "my-home-garage"
    assert body["destination"]["capacity_class"] == "small"
    assert body["applied"] == [
        {"created_location": "my-home"},
        {"created_location": "my-home-garage"},
        {"created_location": "my-home-attic"},
        {"created_location": "my-home-garage-hardware-bin"},
    ]

    tree = client.get("/locations").json()
    assert len(tree["locations"]) == 4
    assert client.get("/locations/my-home-garage-hardware-bin").status_code == 200

    undone = client.post("/assistant/undo").json()
    assert [entry["deleted_location"] for entry in undone["undone"]] == [
        "my-home-garage-hardware-bin",
        "my-home-attic",
        "my-home-garage",
        "my-home",
    ]
    assert client.get("/locations").json() == {"roots": [], "locations": []}


def test_mobile_putaway_uses_the_ready_first_destination_without_selection(
    tmp_path,
) -> None:
    client = product_client(tmp_path)
    setup = client.post(
        "/storage/setup",
        json={
            "root_name": "My Home",
            "root_type": "home",
            "children": [{"label": "Garage", "type": "room"}],
            "destination": {
                "label": "Hardware bin",
                "type": "bin",
                "parent_label": "Garage",
                "capacity_class": "small",
            },
        },
    ).json()
    destination_id = setup["destination"]["id"]

    proposed = client.post(
        "/assistant/chat",
        json={
            "text": "Put away M4 T-nuts",
            "source": "mobile",
        },
    ).json()
    decision = proposed["decision"]

    assert decision["action"] == "put_away"
    assert decision["destination"]["id"] == destination_id
    assert decision["plan"][0]["arguments"]["location_id"] == destination_id
    assert "Hardware bin" in decision["message"]
    assert "matches location name: hardware" in decision["placement"]["reasons"]


def test_putaway_explanation_leads_with_matching_purpose_tags(tmp_path) -> None:
    client = product_client(tmp_path)
    root = client.post(
        "/locations",
        json={"label": "Workshop", "type": "area", "accepts_items": False},
    ).json()["id"]
    electronics = client.post(
        "/locations",
        json={
            "label": "Small Electronics",
            "type": "drawer",
            "parent_id": root,
            "accepts_items": True,
            "capacity_class": "small",
            "purpose_tags": ["electronics", "adapters"],
        },
    ).json()["id"]
    client.post(
        "/locations",
        json={
            "label": "General Hardware",
            "type": "drawer",
            "parent_id": root,
            "accepts_items": True,
            "capacity_class": "small",
            "purpose_tags": ["hardware"],
        },
    )

    proposed = client.post(
        "/assistant/chat",
        json={"text": "Put away USB-C right-angle adapters", "source": "mobile"},
    ).json()["decision"]

    assert proposed["destination"]["id"] == electronics
    assert "matches purpose tags: adapters" in proposed["placement"]["reasons"]
    assert proposed["placement"]["reason"] == (
        "Small Electronics is intended for adapters"
    )
    assert proposed["confirmation"]["explanation"]["summary"] == (
        "Small Electronics is intended for adapters"
    )


def test_putaway_explanation_leads_with_learned_placement_rule(tmp_path) -> None:
    client = product_client(tmp_path)
    cabinet = client.post(
        "/locations",
        json={"label": "Main Cabinet", "type": "cabinet", "accepts_items": False},
    ).json()["id"]
    jewelry = client.post(
        "/locations",
        json={
            "label": "Jewelry Drawer",
            "type": "drawer",
            "parent_id": cabinet,
            "accepts_items": True,
            "capacity_class": "small",
        },
    ).json()["id"]
    store = InventoryStore(tmp_path / "inventory.db")
    store.learn_rule(
        rule_text="Jewelry belongs in the main cabinet.",
        trigger_terms=["jewelry", "bracelet"],
        target_location_id=cabinet,
    )

    proposed = client.post(
        "/assistant/chat",
        json={"text": "Put away a bracelet clasp", "source": "mobile"},
    ).json()["decision"]

    assert proposed["destination"]["id"] == jewelry
    assert proposed["placement"]["reason"] == (
        "Jewelry Drawer is inside the place set by your rule: "
        "Jewelry belongs in the main cabinet."
    )
    assert proposed["confirmation"]["explanation"]["summary"] == (
        proposed["placement"]["reason"]
    )


def test_first_putaway_uses_the_only_empty_usable_location_as_fallback(
    tmp_path,
) -> None:
    client = product_client(tmp_path)
    setup = client.post(
        "/storage/setup",
        json={
            "root_name": "Studio",
            "root_type": "room",
            "children": [],
            "destination": {
                "label": "Seasonal shelf",
                "type": "shelf",
                "capacity_class": "large",
            },
        },
    ).json()
    destination_id = setup["destination"]["id"]

    proposed = client.post(
        "/assistant/chat",
        json={"text": "Put away canvas roll", "source": "mobile"},
    ).json()["decision"]

    assert proposed["action"] == "put_away"
    assert proposed["destination"]["id"] == destination_id
    assert proposed["placement"]["reasons"] == [
        "only empty storage location that fits"
    ]
    assert (
        proposed["placement"]["reason"]
        == "it is the only empty storage location that fits"
    )
    assert proposed["active_finds"][0]["color"] == [255, 0, 0]
    assert proposed["confirmation"]["rows"][0]["color"] == {
        "name": "Red",
        "rgb": [255, 0, 0],
    }
    assert proposed["confirmation"]["rows"][0]["light"] == {
        "location_id": None,
        "status": "unavailable",
    }


def test_selected_cross_root_destination_explanation_names_the_actual_place(
    tmp_path,
) -> None:
    client = product_client(tmp_path)
    workshop = client.post(
        "/locations",
        json={
            "label": "Workshop",
            "type": "area",
            "accepts_items": False,
        },
    ).json()["id"]
    tool_wall = client.post(
        "/locations",
        json={
            "label": "Tool wall",
            "type": "rack",
            "parent_id": workshop,
            "accepts_items": True,
            "capacity_class": "large",
        },
    ).json()["id"]

    proposed = client.post(
        "/assistant/chat",
        json={"text": "Put away a claw hammer in Tool wall", "source": "mobile"},
    ).json()["decision"]

    assert proposed["destination"]["id"] == tool_wall
    assert proposed["placement"]["reasons"] == [
        "using the selected location because it fits"
    ]
    assert proposed["placement"]["reason"] == (
        "you selected Tool wall, and it fits this item"
    )
    assert proposed["confirmation"]["explanation"] == {
        "label": "Why this location?",
        "summary": "you selected Tool wall, and it fits this item",
    }


def test_putaway_explains_when_it_uses_a_learned_home(tmp_path) -> None:
    client = product_client(tmp_path)
    workshop = client.post(
        "/locations",
        json={"label": "Workshop", "type": "area", "accepts_items": False},
    ).json()["id"]
    tool_wall = client.post(
        "/locations",
        json={
            "label": "Tool wall",
            "type": "rack",
            "parent_id": workshop,
            "accepts_items": True,
            "capacity_class": "large",
        },
    ).json()["id"]
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="Claw hammer")
    store.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")
    store.update_item_home(item_id=item_id, location_id=tool_wall)

    proposed = client.post(
        "/assistant/chat",
        json={"text": "Put away my claw hammer", "source": "voice"},
    ).json()["decision"]

    assert proposed["destination"]["id"] == tool_wall
    assert proposed["placement"]["reasons"] == [
        "learned home for this exact item"
    ]
    assert proposed["placement"]["reason"] == (
        "you previously chose Tool wall for this item"
    )
    assert proposed["confirmation"]["explanation"] == {
        "label": "Why this location?",
        "summary": "you previously chose Tool wall for this item",
    }


def test_putaway_explains_a_home_learned_from_similar_items(tmp_path) -> None:
    client = product_client(tmp_path)
    workshop = client.post(
        "/locations",
        json={"label": "Workshop", "type": "area", "accepts_items": False},
    ).json()["id"]
    metric_hardware = client.post(
        "/locations",
        json={
            "label": "Metric T-nuts",
            "type": "bin",
            "parent_id": workshop,
            "accepts_items": True,
            "capacity_class": "small",
        },
    ).json()["id"]
    for name in ("M3 T-nuts", "M4 T-nuts"):
        client.post(
            "/inventory",
            json={
                "name": name,
                "bin_id": metric_hardware,
                "quantity": 10,
                "unit": "pieces",
            },
        )

    proposed = client.post(
        "/assistant/chat",
        json={"text": "Put away M5 T-nuts", "source": "mobile"},
    ).json()["decision"]

    assert proposed["destination"]["id"] == metric_hardware
    assert "learned home from 2 similar items" in proposed["placement"]["reasons"]
    assert proposed["placement"]["reason"] == (
        "you previously chose Metric T-nuts for 2 similar items"
    )
    assert proposed["confirmation"]["explanation"] == {
        "label": "Why this location?",
        "summary": "you previously chose Metric T-nuts for 2 similar items",
    }


def test_first_run_setup_putaway_and_retrieval_are_one_complete_journey(
    tmp_path,
) -> None:
    client = product_client(tmp_path)
    setup = client.post(
        "/storage/setup",
        json={
            "root_name": "My Home",
            "root_type": "home",
            "children": [],
            "destination": {
                "label": "General storage",
                "type": "bin",
                "capacity_class": "medium",
            },
        },
    ).json()
    destination_id = setup["destination"]["id"]

    proposed = client.post(
        "/assistant/chat",
        json={
            "text": "Put away USB cable",
            "current_location_id": destination_id,
            "source": "mobile",
        },
    ).json()
    decision = proposed["decision"]

    assert decision["action"] == "put_away"
    assert decision["destination"]["id"] == destination_id
    assert decision["plan"] == [
        {
            "tool": "put_item",
            "arguments": {
                "item": "Usb Cable",
                "location_id": destination_id,
                "quantity": 1.0,
                "unit": "each",
                "size_class": "small",
            },
        }
    ]

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "plan": decision["plan"],
            "command": "Put away USB cable",
            "source_interaction_id": proposed["interaction_id"],
        },
    ).json()
    assert confirmed["applied"][0]["stored"] == "Usb Cable"

    inventory = client.get("/inventory").json()["items"]
    assert [(item["name"], item["bin_id"]) for item in inventory] == [
        ("Usb Cable", destination_id)
    ]

    found = client.post("/assistant/find", json={"query": "USB cable"}).json()
    assert found["lit"] is False
    assert found["results"][0]["location_id"] == destination_id
    assert [node["label"] for node in found["results"][0]["path"]] == [
        "My Home",
        "General storage",
    ]


def test_inventory_catalog_groups_filters_and_pages_items(tmp_path) -> None:
    client = product_client(tmp_path)
    attic = client.post(
        "/locations",
        json={"type": "room", "label": "Attic"},
    ).json()["id"]
    tote = client.post(
        "/locations",
        json={
            "type": "box",
            "label": "Parts Tote",
            "parent_id": attic,
            "accepts_items": True,
        },
    ).json()["id"]
    store = InventoryStore(tmp_path / "inventory.db")

    m2_id = store.upsert_item(name="M2 bolts", category="hardware")
    store.put_inventory(
        item_id=m2_id,
        bin_id="B001",
        quantity=2,
        unit="each",
        normalize_bin=False,
    )
    store.put_inventory(
        item_id=m2_id,
        bin_id=tote,
        quantity=3,
        unit="each",
        normalize_bin=False,
    )
    m10_id = store.upsert_item(name="M10 bolts", category="hardware")
    store.put_inventory(
        item_id=m10_id,
        bin_id="B001",
        quantity=10,
        unit="each",
        normalize_bin=False,
    )
    cable_id = store.upsert_item(
        name="USB cable",
        category="electronics",
        aliases=["phone lead"],
    )
    store.put_inventory(
        item_id=cable_id,
        bin_id=tote,
        quantity=1,
        unit="each",
        normalize_bin=False,
    )
    clip_id = store.upsert_item(name="Mystery clip")
    store.put_inventory(
        item_id=clip_id,
        bin_id=tote,
        quantity=1,
        unit="each",
        needs_review=True,
        review_reason="Confirm item",
        normalize_bin=False,
    )
    photo = store.add_photo(
        filename="clip.jpg",
        content_type="image/jpeg",
        data=b"photo",
    )
    store.attach_photo(item_id=clip_id, photo_id=photo.id)
    archived_id = store.upsert_item(name="Retired jig", category="tools")
    store.put_inventory(
        item_id=archived_id,
        bin_id="B001",
        quantity=1,
        unit="each",
        normalize_bin=False,
    )
    assert store.archive_item(archived_id) is True

    first = client.get(
        "/inventory/catalog",
        params={"q": "bolts", "sort": "name", "limit": 1},
    ).json()
    second = client.get(
        "/inventory/catalog",
        params={"q": "bolts", "sort": "name", "limit": 1, "offset": 1},
    ).json()

    assert first["schema"] == "magpie.inventory_catalog.v1"
    assert first["total"] == 2
    assert first["count"] == 1
    assert first["has_more"] is True
    assert first["groups"][0]["name"] == "M2 bolts"
    assert len(first["groups"][0]["placements"]) == 2
    assert len(first["items"]) == 2
    assert second["groups"][0]["name"] == "M10 bolts"
    assert second["has_more"] is False
    assert first["metrics"] == {
        "items": 4,
        "placements": 5,
        "locations": 2,
        "need_review": 1,
    }
    assert first["categories"] == ["electronics", "hardware"]

    by_location = client.get(
        "/inventory/catalog",
        params={"q": "Attic › Parts Tote"},
    ).json()
    assert {group["name"] for group in by_location["groups"]} == {
        "M2 bolts",
        "Mystery clip",
        "USB cable",
    }
    m2_group = next(
        group for group in by_location["groups"] if group["name"] == "M2 bolts"
    )
    tote_placement = next(
        placement
        for placement in m2_group["placements"]
        if placement["bin_id"] == tote
    )
    assert tote_placement["location_path"] == [
        {"id": attic, "label": "Attic"},
        {"id": tote, "label": "Parts Tote"},
    ]
    by_root = client.get(
        "/inventory/catalog",
        params={"root_id": attic},
    ).json()
    assert by_root["root_id"] == attic
    assert by_root["root_label"] == "Attic"
    assert {group["name"] for group in by_root["groups"]} == {
        "M2 bolts",
        "Mystery clip",
        "USB cable",
    }
    scoped_m2 = next(
        group for group in by_root["groups"] if group["name"] == "M2 bolts"
    )
    assert {placement["bin_id"] for placement in scoped_m2["placements"]} == {
        "B001",
        tote,
    }
    assert by_root["metrics"] == {
        "items": 3,
        "placements": 3,
        "locations": 1,
        "need_review": 1,
    }
    assert client.get(
        "/inventory/catalog",
        params={"root_id": tote},
    ).status_code == 404

    reviewed = client.get(
        "/inventory/catalog",
        params={"scope": "review"},
    ).json()
    photographed = client.get(
        "/inventory/catalog",
        params={"scope": "photos"},
    ).json()
    uncategorized = client.get(
        "/inventory/catalog",
        params={"scope": "uncategorized"},
    ).json()
    alias = client.get(
        "/inventory/catalog",
        params={"q": "phone lead", "category": "electronics"},
    ).json()
    archived = client.get(
        "/inventory/catalog",
        params={"archived": "true"},
    ).json()

    assert [group["name"] for group in reviewed["groups"]] == ["Mystery clip"]
    assert [group["name"] for group in photographed["groups"]] == ["Mystery clip"]
    assert [group["name"] for group in uncategorized["groups"]] == ["Mystery clip"]
    assert [group["name"] for group in alias["groups"]] == ["USB cable"]
    assert [group["name"] for group in archived["groups"]] == ["Retired jig"]


def test_inventory_catalog_reports_active_and_archived_placement_state(
    tmp_path,
) -> None:
    client = product_client(tmp_path)
    attic = client.post(
        "/locations",
        json={"type": "room", "label": "Attic"},
    ).json()["id"]
    tote = client.post(
        "/locations",
        json={
            "type": "box",
            "label": "Winter tote",
            "parent_id": attic,
            "accepts_items": True,
        },
    ).json()["id"]
    active_bin = client.post(
        "/locations",
        json={
            "type": "box",
            "label": "Desk bin",
            "accepts_items": True,
        },
    ).json()["id"]
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="USB adapters", category="electronics")
    store.put_inventory(
        item_id=item_id,
        bin_id=active_bin,
        quantity=2,
        unit="each",
        normalize_bin=False,
    )
    store.put_inventory(
        item_id=item_id,
        bin_id=tote,
        quantity=1,
        unit="each",
        normalize_bin=False,
    )

    assert client.post(f"/locations/{attic}/archive").status_code == 200
    catalog = client.get("/inventory/catalog").json()
    group = catalog["groups"][0]
    placements = {row["bin_id"]: row for row in group["placements"]}

    assert group["active"] is True
    assert placements[active_bin]["location_active"] is True
    assert placements[active_bin]["location_archived"] is False
    assert placements[active_bin]["location_missing"] is False
    assert placements[tote]["location_active"] is False
    assert placements[tote]["location_archived"] is True
    assert placements[tote]["location_missing"] is False
    assert placements[tote]["location_path_label"] == "Attic › Winter tote"


def test_inventory_catalog_surfaces_and_clears_legacy_placement_issues(
    tmp_path,
) -> None:
    client = product_client(tmp_path)
    attic = client.post(
        "/locations",
        json={"type": "room", "label": "Attic"},
    ).json()["id"]
    tote = client.post(
        "/locations",
        json={"type": "tote", "label": "Parts Tote", "parent_id": attic},
    ).json()["id"]
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="Heirloom jewelry")
    store.put_inventory(
        item_id=item_id,
        bin_id=attic,
        quantity=1,
        unit="box",
        normalize_bin=False,
    )

    catalog = client.get(
        "/inventory/catalog",
        params={"scope": "placement"},
    ).json()

    assert catalog["scope"] == "placement"
    assert catalog["total"] == 1
    assert catalog["metrics"] == {
        "items": 1,
        "placements": 1,
        "locations": 1,
        "need_review": 0,
    }
    assert catalog["groups"][0]["placement_issue_count"] == 1
    [placement] = catalog["groups"][0]["placements"]
    assert placement["bin_id"] == attic
    assert placement["location_accepts_items"] is False
    assert placement["placement_issue"] == "organizer_only"

    moved = client.post(
        "/inventory/move",
        json={"item_id": item_id, "from_bin": attic, "to_bin": tote},
    )

    assert moved.status_code == 200
    assert client.get(
        "/inventory/catalog",
        params={"scope": "placement"},
    ).json()["groups"] == []


def test_inventory_catalog_repairs_inventory_from_a_missing_location(
    tmp_path,
) -> None:
    client = product_client(tmp_path)
    tote = client.post(
        "/locations",
        json={"type": "tote", "label": "Recovered Items"},
    ).json()["id"]
    store = InventoryStore(tmp_path / "inventory.db")
    item_id = store.upsert_item(name="Orphaned camera cable")
    store.put_inventory(
        item_id=item_id,
        bin_id="removed-shelf",
        quantity=1,
        unit="each",
        normalize_bin=False,
    )

    catalog = client.get(
        "/inventory/catalog",
        params={"scope": "placement"},
    ).json()

    assert catalog["total"] == 1
    [placement] = catalog["groups"][0]["placements"]
    assert placement["bin_id"] == "removed-shelf"
    assert placement["location_missing"] is True
    assert placement["location_accepts_items"] is None
    assert placement["placement_issue"] == "missing_location"

    moved = client.post(
        "/inventory/move",
        json={
            "item_id": item_id,
            "from_bin": "removed-shelf",
            "to_bin": tote,
        },
    )

    assert moved.status_code == 200
    assert moved.json()["applied"][0]["from_location_id"] == "removed-shelf"
    assert client.get(
        "/inventory/catalog",
        params={"scope": "placement"},
    ).json()["groups"] == []

    rejected = client.post(
        "/inventory/move",
        json={
            "item_id": item_id,
            "from_bin": "made-up-source",
            "to_bin": tote,
        },
    )
    assert rejected.status_code == 400
    assert "unknown source location" in rejected.json()["detail"]


def test_unlit_find_session_persists_across_surfaces_until_cleared(tmp_path) -> None:
    client = product_client(tmp_path)
    setup = client.post(
        "/storage/setup",
        json={
            "root_name": "My Home",
            "root_type": "home",
            "children": [],
            "destination": {
                "label": "Garage tote",
                "type": "box",
                "capacity_class": "large",
            },
        },
    ).json()
    root_id = setup["root"]["id"]
    garage_tote_id = setup["destination"]["id"]
    attic_tote_id = client.post(
        "/locations",
        json={
            "label": "Attic tote",
            "type": "box",
            "parent_id": root_id,
            "capacity_class": "large",
        },
    ).json()["id"]
    client.post(
        "/inventory",
        json={"name": "USB drives", "bin_id": garage_tote_id},
    )
    client.post(
        "/inventory",
        json={"name": "USB dongles", "bin_id": attic_tote_id},
    )

    first = client.post("/assistant/find", json={"query": "USB drives"}).json()
    second = client.post("/assistant/find", json={"query": "USB dongles"}).json()
    active = client.get("/lights/active").json()["active_finds"]

    assert first["lit"] is False
    assert first["light_assignments"] == []
    assert second["lit"] is False
    assert second["light_assignments"] == []
    assert [entry["query"] for entry in active] == ["USB drives", "USB dongles"]
    assert [entry["color"] for entry in active] == [[255, 0, 0], [0, 70, 255]]
    assert active[0]["matches"] == [
        {
            "item_id": first["results"][0]["item_id"],
            "name": "USB drives",
            "location_id": garage_tote_id,
            "lit_location": None,
            "path": [
                {"id": root_id, "label": "My Home"},
                {"id": garage_tote_id, "label": "Garage tote"},
            ],
        }
    ]
    assert active[1]["matches"][0]["location_id"] == attic_tote_id
    assert active[1]["matches"][0]["lit_location"] is None
    assert client.get("/voice/status").json()["voice"]["active_finds"] == active

    cleared = client.post("/clear").json()
    assert cleared["active_finds"] == []
    assert cleared["message"] == "Current finds and lights cleared."
    assert client.get("/lights/active").json()["active_finds"] == []


def test_active_find_path_tracks_a_moved_container(tmp_path) -> None:
    client = product_client(tmp_path)
    home = client.post(
        "/locations",
        json={"label": "My Home", "type": "home"},
    ).json()["id"]
    tote = client.post(
        "/locations",
        json={
            "label": "Cable tote",
            "type": "box",
            "parent_id": home,
            "accepts_items": True,
        },
    ).json()["id"]
    workshop = client.post(
        "/locations",
        json={"label": "Workshop", "type": "room"},
    ).json()["id"]
    client.post(
        "/inventory",
        json={"name": "USB adapters", "bin_id": tote},
    )
    found = client.post(
        "/assistant/find",
        json={"query": "USB adapters"},
    ).json()

    assert [
        node["label"]
        for node in found["active_finds"][0]["matches"][0]["path"]
    ] == ["My Home", "Cable tote"]
    moved = client.post(
        f"/locations/{tote}/move",
        json={"new_parent_id": workshop},
    )

    assert moved.status_code == 200
    active = moved.json()["active_finds"]
    assert moved.json()["light_ok"] is True
    assert moved.json()["light_errors"] == []
    assert [node["label"] for node in active[0]["matches"][0]["path"]] == [
        "Workshop",
        "Cable tote",
    ]
    assert client.get("/lights/active").json()["active_finds"] == active
    assert client.get("/voice/status").json()["voice"]["active_finds"] == active


def test_archiving_a_location_removes_only_its_active_find_matches(
    tmp_path,
) -> None:
    client = product_client(tmp_path)
    home = client.post(
        "/locations",
        json={"label": "My Home", "type": "home"},
    ).json()["id"]
    cable_tote = client.post(
        "/locations",
        json={
            "label": "Cable tote",
            "type": "box",
            "parent_id": home,
            "accepts_items": True,
        },
    ).json()["id"]
    tool_tote = client.post(
        "/locations",
        json={
            "label": "Tool tote",
            "type": "box",
            "parent_id": home,
            "accepts_items": True,
        },
    ).json()["id"]
    client.post(
        "/inventory",
        json={"name": "USB adapters", "bin_id": cable_tote},
    )
    client.post(
        "/inventory",
        json={"name": "Blue pry tool", "bin_id": tool_tote},
    )
    first = client.post(
        "/assistant/find",
        json={"query": "USB adapters"},
    ).json()
    second = client.post(
        "/assistant/find",
        json={"query": "Blue pry tool"},
    ).json()

    archived = client.post(f"/locations/{cable_tote}/archive")

    assert archived.status_code == 200
    active = archived.json()["active_finds"]
    assert archived.json()["light_ok"] is True
    assert archived.json()["light_errors"] == []
    assert len(active) == 1
    assert active[0]["id"] == second["active_finds"][-1]["id"]
    assert active[0]["query"] == "Blue pry tool"
    assert active[0]["color"] == [0, 70, 255]
    assert active[0]["matches"][0]["location_id"] == tool_tote
    assert all(
        match["location_id"] != cable_tote
        for entry in active
        for match in entry["matches"]
    )
    assert first["active_finds"][0]["query"] == "USB adapters"
    assert client.get("/lights/active").json()["active_finds"] == active

    restored = client.post(f"/locations/{cable_tote}/restore").json()
    assert restored["active_finds"] == active


def test_moving_inventory_removes_its_stale_active_find(tmp_path) -> None:
    client = product_client(tmp_path)
    home = client.post(
        "/locations",
        json={"label": "My Home", "type": "home"},
    ).json()["id"]
    source = client.post(
        "/locations",
        json={
            "label": "Cable tote",
            "type": "box",
            "parent_id": home,
            "accepts_items": True,
        },
    ).json()["id"]
    destination = client.post(
        "/locations",
        json={
            "label": "Desk bin",
            "type": "bin",
            "parent_id": home,
            "accepts_items": True,
        },
    ).json()["id"]
    stored = client.post(
        "/inventory",
        json={"name": "USB adapters", "bin_id": source},
    ).json()
    found = client.post(
        "/assistant/find",
        json={"query": "USB adapters"},
    ).json()

    assert found["active_finds"]
    moved = client.post(
        "/inventory/move",
        json={
            "item_id": stored["item_id"],
            "from_bin": source,
            "to_bin": destination,
        },
    ).json()

    assert moved["active_finds"] == []
    assert moved["active_find_light_ok"] is True
    assert moved["active_find_light_errors"] == []
    assert client.get("/lights/active").json()["active_finds"] == []
    refound = client.post(
        "/assistant/find",
        json={"query": "USB adapters"},
    ).json()
    assert refound["active_finds"][0]["matches"][0]["location_id"] == destination


def test_moving_confirmed_putaway_updates_its_active_location(tmp_path) -> None:
    client = product_client(tmp_path)
    house = client.post(
        "/locations",
        json={"label": "House", "type": "home"},
    ).json()["id"]
    source = client.post(
        "/locations",
        json={
            "label": "Seasonal box",
            "type": "box",
            "parent_id": house,
            "accepts_items": True,
        },
    ).json()["id"]
    workshop = client.post(
        "/locations",
        json={"label": "Workshop", "type": "area"},
    ).json()["id"]
    destination = client.post(
        "/locations",
        json={
            "label": "Hardware bin",
            "type": "bin",
            "parent_id": workshop,
            "accepts_items": True,
        },
    ).json()["id"]

    proposed = client.post(
        "/assistant/chat",
        json={
            "text": "Put away holiday string lights",
            "current_location_id": source,
            "source": "mobile",
        },
    ).json()
    decision = proposed["decision"]
    confirmed = client.post(
        "/assistant/confirm",
        json={
            "plan": decision["plan"],
            "command": "Put away holiday string lights",
            "source_interaction_id": proposed["interaction_id"],
        },
    ).json()
    item_id = confirmed["applied"][0]["item_id"]
    [active_putaway] = confirmed["active_finds"]
    assert active_putaway["kind"] == "put_away"
    assert active_putaway["matches"][0]["item_id"] == item_id
    assert active_putaway["matches"][0]["location_id"] == source

    moved = client.post(
        "/inventory/move",
        json={
            "item_id": item_id,
            "from_bin": source,
            "to_bin": destination,
        },
    ).json()

    [active_putaway] = moved["active_finds"]
    [match] = active_putaway["matches"]
    assert active_putaway["query"] == "holiday string lights"
    assert match["item_id"] == item_id
    assert match["location_id"] == destination
    assert [node["label"] for node in match["path"]] == [
        "Workshop",
        "Hardware bin",
    ]
    assert client.get("/lights/active").json()["active_finds"] == [
        active_putaway
    ]


def test_not_here_marks_only_the_failed_placement_for_review_and_learns(
    tmp_path,
) -> None:
    client = product_client(tmp_path)
    setup = client.post(
        "/storage/setup",
        json={
            "root_name": "Workshop",
            "root_type": "room",
            "children": [],
            "destination": {
                "label": "Fastener drawer",
                "type": "drawer",
                "capacity_class": "small",
            },
        },
    ).json()
    drawer_id = setup["destination"]["id"]
    stored = client.post(
        "/inventory",
        json={"name": "M4 T-nuts", "bin_id": drawer_id, "quantity": 24},
    ).json()

    found = client.post("/assistant/find", json={"query": "M4 T-nuts"}).json()
    active = found["active_finds"][0]
    correction = client.post(
        "/assistant/find/not-here",
        json={
            "item_id": stored["item_id"],
            "location_id": drawer_id,
            "query": "M4 T-nuts",
            "active_find_id": active["id"],
            "source": "mobile",
        },
    )

    assert correction.status_code == 200
    body = correction.json()
    assert body["action"] == "find_not_here"
    assert body["item"] == "M4 T-nuts"
    assert body["location_id"] == drawer_id
    assert body["needs_review"] is True
    assert body["active_find_removed"] is True
    assert body["active_finds"] == []
    assert client.get("/lights/active").json()["active_finds"] == []

    review = client.get("/inventory/review").json()["items"]
    assert [(row["item_id"], row["bin_id"]) for row in review] == [
        (stored["item_id"], drawer_id)
    ]
    assert review[0]["review_reason"] == "Reported missing during retrieval."

    repeated_find = client.post(
        "/assistant/find",
        json={"query": "M4 T-nuts"},
    ).json()
    assert "was last recorded" in repeated_find["message"]
    assert "needs review" in repeated_find["message"]
    typed_correction = client.post(
        "/assistant/chat",
        json={"text": "the M4 T-nuts are not here", "source": "ui"},
    ).json()
    assert typed_correction["decision"]["action"] == "find_not_here"
    assert typed_correction["decision"]["active_finds"] == []

    examples = InventoryStore(tmp_path / "inventory.db").examples_for_eval(
        limit=10,
        kind="correction",
    )
    assert len(examples) == 1
    learned = json.loads(examples[0]["action"])
    assert learned["find_not_here"] == {
        "item_id": stored["item_id"],
        "item": "M4 T-nuts",
        "location_id": drawer_id,
    }


def test_voice_not_here_repairs_a_single_active_find(tmp_path) -> None:
    client = product_client(tmp_path)
    destination = client.post(
        "/locations",
        json={
            "label": "Electronics tote",
            "type": "box",
            "capacity_class": "medium",
        },
    ).json()["id"]
    stored = client.post(
        "/inventory",
        json={"name": "USB adapters", "bin_id": destination},
    ).json()
    client.post("/assistant/find", json={"query": "USB adapters"})

    response = client.post(
        "/assistant/chat",
        json={"text": "not here", "source": "voice"},
    ).json()

    assert response["decision"]["action"] == "find_not_here"
    assert response["decision"]["item_id"] == stored["item_id"]
    assert response["decision"]["location_id"] == destination
    assert response["decision"]["active_finds"] == []
    assert response["source"] == "voice"
    review = client.get("/inventory/review").json()["items"]
    assert review[0]["item_id"] == stored["item_id"]


def test_moving_a_missing_review_resolves_and_undo_restores_the_uncertainty(
    tmp_path,
) -> None:
    client = product_client(tmp_path)
    workshop = client.post(
        "/locations",
        json={"label": "Workshop", "type": "room"},
    ).json()["id"]
    source = client.post(
        "/locations",
        json={
            "label": "Fastener drawer",
            "type": "drawer",
            "parent_id": workshop,
            "capacity_class": "small",
        },
    ).json()["id"]
    destination = client.post(
        "/locations",
        json={
            "label": "Hardware tote",
            "type": "box",
            "parent_id": workshop,
            "capacity_class": "medium",
        },
    ).json()["id"]
    stored = client.post(
        "/inventory",
        json={"name": "M3 washers", "bin_id": source, "quantity": 30},
    ).json()
    found = client.post("/assistant/find", json={"query": "M3 washers"}).json()
    client.post(
        "/assistant/find/not-here",
        json={
            "item_id": stored["item_id"],
            "location_id": source,
            "active_find_id": found["active_finds"][0]["id"],
            "query": "M3 washers",
        },
    )

    preview = client.post(
        "/inventory/move-preview",
        json={
            "entries": [{"item_id": stored["item_id"], "from_bin": source}],
            "to_bin": destination,
        },
    ).json()
    assert preview["review_resolution_count"] == 1
    assert preview["entries"][0]["resolves_review"] is True

    moved = client.post(
        "/inventory/move",
        json={
            "item_id": stored["item_id"],
            "from_bin": source,
            "to_bin": destination,
        },
    )
    assert moved.status_code == 200
    assert moved.json()["resolved_review_count"] == 1
    assert client.get("/inventory/review").json()["items"] == []
    [placement] = InventoryStore(tmp_path / "inventory.db").item_inventory(
        stored["item_id"]
    )
    assert placement.bin_id == destination
    assert placement.needs_review is False

    examples = InventoryStore(tmp_path / "inventory.db").examples_for_eval(
        limit=10,
        kind="review_correction",
    )
    learned = json.loads(examples[0]["action"])
    assert examples[0]["command"] == (
        f"review corrected M3 washers from {source} to {destination}"
    )
    assert learned["before"]["bin_id"] == source
    assert learned["before"]["needs_review"] is True
    assert learned["after"]["bin_id"] == destination
    assert learned["after"]["needs_review"] is False

    undone = client.post("/assistant/undo").json()
    assert undone["ok"] is True
    [restored] = InventoryStore(tmp_path / "inventory.db").item_inventory(
        stored["item_id"]
    )
    assert restored.bin_id == source
    assert restored.needs_review is True
    assert restored.review_reason == "Reported missing during retrieval."


def test_guided_storage_setup_validates_before_writing(tmp_path) -> None:
    client = product_client(tmp_path)

    duplicate = client.post(
        "/storage/setup",
        json={
            "root_name": "Workshop",
            "root_type": "area",
            "children": [
                {"label": "Parts", "type": "area"},
                {"label": " parts ", "type": "area"},
            ],
        },
    )
    assert duplicate.status_code == 400
    assert duplicate.json()["detail"] == "starter place names must be unique"
    assert client.get("/locations").json() == {"roots": [], "locations": []}

    blank = client.post(
        "/storage/setup",
        json={
            "root_name": "   ",
            "root_type": "home",
            "children": [{"label": "Garage", "type": "room"}],
        },
    )
    assert blank.status_code == 400
    assert blank.json()["detail"] == "storage place names cannot be blank"
    assert client.get("/locations").json() == {"roots": [], "locations": []}

    unknown_parent = client.post(
        "/storage/setup",
        json={
            "root_name": "My Home",
            "root_type": "home",
            "children": [{"label": "Garage", "type": "room"}],
            "destination": {
                "label": "Hardware bin",
                "type": "bin",
                "parent_label": "Attic",
            },
        },
    )
    assert unknown_parent.status_code == 400
    assert (
        unknown_parent.json()["detail"]
        == "first container parent must be one of the starter places"
    )
    assert client.get("/locations").json() == {"roots": [], "locations": []}

    first = client.post(
        "/storage/setup",
        json={
            "root_name": "My Home",
            "root_type": "home",
            "children": [{"label": "Garage", "type": "room"}],
        },
    )
    assert first.status_code == 200

    second = client.post(
        "/storage/setup",
        json={
            "root_name": "Workshop",
            "root_type": "area",
            "children": [{"label": "Bench", "type": "area"}],
            "destination": {
                "label": "Fasteners",
                "type": "bin",
                "parent_label": "Bench",
            },
        },
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["root"]["id"] == "workshop"
    assert second_body["destination"]["parent_id"] == "workshop-bench"
    assert client.get("/locations").json()["roots"] == ["my-home", "workshop"]

    duplicate_root = client.post(
        "/storage/setup",
        json={"root_name": " workshop ", "root_type": "area"},
    )
    assert duplicate_root.status_code == 409
    assert (
        duplicate_root.json()["detail"]
        == "a top-level storage place with that name already exists"
    )

    undone = client.post("/assistant/undo").json()
    assert [entry["deleted_location"] for entry in undone["undone"]] == [
        "workshop-bench-fasteners",
        "workshop-bench",
        "workshop",
    ]
    remaining = client.get("/locations").json()
    assert remaining["roots"] == ["my-home"]
    assert {location["id"] for location in remaining["locations"]} == {
        "my-home",
        "my-home-garage",
    }


def test_manual_location_save_moves_atomically_and_restores_full_state(tmp_path) -> None:
    client = product_client(tmp_path)
    garage_id = client.post(
        "/locations", json={"type": "room", "label": "Garage"}
    ).json()["id"]
    basement_id = client.post(
        "/locations", json={"type": "room", "label": "Basement"}
    ).json()["id"]
    cabinet_id = client.post(
        "/locations",
        json={
            "type": "cabinet",
            "label": "Printer Cabinet",
            "parent_id": garage_id,
            "cols": 2,
            "rows": 2,
            "capacity_class": "large",
            "purpose_tags": ["printer"],
            "placement_notes": "Filament and printer tools.",
            "internal_dimensions": {
                "width_mm": 700,
                "depth_mm": 400,
                "height_mm": 900,
            },
        },
    ).json()["id"]

    saved = client.post(
        f"/locations/{cabinet_id}/update",
        json={
            "label": "Electronics Cabinet",
            "type": "locker",
            "parent_id": basement_id,
            "cols": 3,
            "rows": 4,
            "capacity_class": "medium",
            "purpose_tags": ["electronics", "components"],
            "placement_notes": "Dry electronics storage.",
            "internal_dimensions": {
                "width_mm": 650,
                "depth_mm": 350,
                "height_mm": 800,
            },
        },
    ).json()

    assert saved["action"] == "manual_update_location"
    assert saved["interaction_id"] > 0
    assert [entry.keys() & {"updated_location", "moved_container"} for entry in saved["applied"]] == [
        {"updated_location"},
        {"moved_container"},
    ]
    updated = client.get(f"/locations/{cabinet_id}").json()["location"]
    assert updated["label"] == "Electronics Cabinet"
    assert updated["parent_id"] == basement_id
    assert updated["grid"] == {"cols": 3, "rows": 4}
    assert updated["internal_dimensions"] == {
        "width_mm": 650.0,
        "depth_mm": 350.0,
        "height_mm": 800.0,
    }

    undone = client.post("/assistant/undo").json()

    assert undone["ok"] is True
    assert {next(iter(row)) for row in undone["undone"]} == {
        "moved_container",
        "restored_location",
    }
    restored = client.get(f"/locations/{cabinet_id}").json()["location"]
    assert restored["label"] == "Printer Cabinet"
    assert restored["type"] == "cabinet"
    assert restored["parent_id"] == garage_id
    assert restored["grid"] == {"cols": 2, "rows": 2}
    assert restored["capacity_class"] == "large"
    assert restored["purpose_tags"] == ["printer"]
    assert restored["placement_notes"] == "Filament and printer tools."
    assert restored["internal_dimensions"] == {
        "width_mm": 700.0,
        "depth_mm": 400.0,
        "height_mm": 900.0,
    }


def test_manual_location_save_rejects_cycle_without_partial_update(tmp_path) -> None:
    client = product_client(tmp_path)
    root_id = client.post(
        "/locations", json={"type": "room", "label": "Garage"}
    ).json()["id"]
    child_id = client.post(
        "/locations",
        json={"type": "cabinet", "label": "Tool Cabinet", "parent_id": root_id},
    ).json()["id"]

    response = client.post(
        f"/locations/{root_id}/update",
        json={"label": "Changed Garage", "parent_id": child_id},
    )

    assert response.status_code == 400
    unchanged = client.get(f"/locations/{root_id}").json()["location"]
    assert unchanged["label"] == "Garage"
    assert unchanged["parent_id"] is None


def test_location_removal_requires_contents_to_be_moved_first(tmp_path) -> None:
    client = product_client(tmp_path)
    root_id = client.post(
        "/locations",
        json={"type": "cabinet", "label": "Printer Cabinet"},
    ).json()["id"]
    child_id = client.post(
        "/locations",
        json={"type": "drawer", "label": "Tools", "parent_id": root_id},
    ).json()["id"]
    client.post(
        f"/locations/{child_id}/items",
        json={"name": "Nozzle wrench", "quantity": 1},
    )

    preview = client.get(f"/locations/{root_id}/removal-preview").json()
    blocked = client.delete(f"/locations/{root_id}")

    assert preview["can_remove"] is False
    assert preview["inventory_count"] == 1
    assert blocked.status_code == 409
    assert "Move contents" in blocked.json()["detail"]["message"]
    assert client.get(f"/locations/{root_id}").status_code == 200

    client.delete(f"/inventory/1/locations/{child_id}")
    removed = client.delete(f"/locations/{root_id}")

    assert removed.status_code == 200
    assert set(removed.json()["removed_ids"]) == {root_id, child_id}
    assert client.get(f"/locations/{root_id}").status_code == 404


def test_storage_manager_exposes_root_and_bin_workflows(tmp_path) -> None:
    client = product_client(tmp_path)

    response = client.get("/storage")

    assert response.status_code == 200
    html = response.text
    assert "Storage spaces" in html
    assert "Build the physical map Magpie uses for put-away and retrieval." in html
    assert 'id="addRoot"' in html
    assert 'id="addChild"' in html
    assert 'id="detailAddMany"' in html
    assert "/children/batch" in html
    assert "/removal-preview" in html
    assert 'id="showArchived"' in html
    assert "/archive" in html
    assert "/restore" in html
    assert '<script src="/product-shell.js?v=' in html
    assert 'data-product-nav aria-label="Product views"' in html


def test_storage_search_finds_locations_and_items_across_roots(tmp_path) -> None:
    client = product_client(tmp_path)
    garage_id = client.post(
        "/locations", json={"type": "room", "label": "Garage"}
    ).json()["id"]
    cabinet_id = client.post(
        "/locations",
        json={
            "type": "cabinet",
            "label": "Printer Cabinet",
            "parent_id": garage_id,
            "purpose_tags": ["filament", "printing"],
            "accepts_items": True,
        },
    ).json()["id"]
    client.post(
        f"/locations/{cabinet_id}/items",
        json={"name": "Brass printer nozzles", "quantity": 3},
    )

    location_search = client.get("/storage/search", params={"q": "printer cabinet"})
    room_search = client.get("/storage/search", params={"q": "garage"})
    path_search = client.get("/storage/search", params={"q": "garage printer"})
    tag_search = client.get("/storage/search", params={"q": "filament"})
    item_search = client.get("/storage/search", params={"q": "printer nozzles"})

    assert location_search.status_code == 200
    assert location_search.json()["location_count"] == 1
    location = location_search.json()["results"][0]
    assert location["result_type"] == "location"
    assert location["location_id"] == cabinet_id
    assert location["accepts_items"] is True
    assert [node["label"] for node in location["path"]] == [
        "Garage",
        "Printer Cabinet",
    ]
    assert [result["location_id"] for result in path_search.json()["results"]] == [
        cabinet_id
    ]
    assert [result["location_id"] for result in tag_search.json()["results"]] == [
        cabinet_id
    ]
    garage = next(
        result
        for result in room_search.json()["results"]
        if result["location_id"] == garage_id
    )
    assert garage["accepts_items"] is False
    assert item_search.status_code == 200
    assert item_search.json()["item_count"] == 1
    item = item_search.json()["results"][0]
    assert item["result_type"] == "item"
    assert item["name"] == "Brass printer nozzles"
    assert item["location_id"] == cabinet_id


def test_storage_search_respects_active_and_archived_views(tmp_path) -> None:
    client = product_client(tmp_path)
    tote_id = client.post(
        "/locations", json={"type": "tote", "label": "Winter Clothes"}
    ).json()["id"]
    client.post(
        f"/locations/{tote_id}/items",
        json={"name": "Winter gloves", "quantity": 2},
    )
    client.post(f"/locations/{tote_id}/archive")

    active = client.get("/storage/search", params={"q": "winter"}).json()
    archived = client.get(
        "/storage/search", params={"q": "winter", "archived": True}
    ).json()

    assert active["results"] == []
    assert {result["result_type"] for result in archived["results"]} == {
        "location",
        "item",
    }
    assert all(result["archived"] is True for result in archived["results"])

    empty = client.get("/storage/search", params={"q": "  "}).json()
    assert empty["results"] == []
    assert empty["location_count"] == 0
    assert empty["item_count"] == 0


def test_location_archive_preserves_inventory_and_restores_subtree(tmp_path) -> None:
    client = product_client(tmp_path)
    root_id = client.post(
        "/locations", json={"type": "area", "label": "Attic"}
    ).json()["id"]
    tote_id = client.post(
        "/locations",
        json={"type": "tote", "label": "Winter tote", "parent_id": root_id},
    ).json()["id"]
    client.post(
        f"/locations/{tote_id}/items",
        json={"name": "Holiday lights", "quantity": 2},
    )

    archived = client.post(f"/locations/{root_id}/archive")

    assert archived.status_code == 200
    assert set(archived.json()["archived_ids"]) == {root_id, tote_id}
    assert client.get("/locations").json() == {"roots": [], "locations": []}
    archived_tree = client.get("/locations?include_archived=true").json()
    assert {row["id"] for row in archived_tree["locations"]} == {root_id, tote_id}
    assert all(row["active"] is False for row in archived_tree["locations"])
    assert client.get(f"/locations/{tote_id}").status_code == 404

    restored = client.post(f"/locations/{root_id}/restore")

    assert restored.status_code == 200
    assert set(restored.json()["restored_ids"]) == {root_id, tote_id}
    assert client.get(f"/locations/{tote_id}").json()["items"][0]["name"] == "Holiday lights"


def test_manual_location_archive_and_restore_are_undoable(tmp_path) -> None:
    client = product_client(tmp_path)
    root_id = client.post(
        "/locations", json={"type": "area", "label": "Garage overflow"}
    ).json()["id"]
    child_id = client.post(
        "/locations",
        json={"type": "tote", "label": "Blue tote", "parent_id": root_id},
    ).json()["id"]
    client.post(
        f"/locations/{child_id}/items",
        json={"name": "Camping stove", "quantity": 1},
    )

    archived = client.post(f"/locations/{root_id}/archive").json()

    assert archived["action"] == "manual_archive_location"
    assert archived["interaction_id"] > 0
    assert archived["applied"] == [
        {
            "archived_location_id": root_id,
            "archived_ids": [root_id, child_id],
        }
    ]
    assert client.get(f"/locations/{root_id}").status_code == 404

    archive_undo = client.post("/assistant/undo").json()

    assert archive_undo["ok"] is True
    assert archive_undo["undone"][0]["restored_archived_location"] == root_id
    assert client.get(f"/locations/{child_id}").json()["items"][0]["name"] == "Camping stove"

    client.post(f"/locations/{root_id}/archive")
    restored = client.post(f"/locations/{root_id}/restore").json()

    assert restored["action"] == "manual_restore_location"
    assert restored["interaction_id"] > archived["interaction_id"]
    assert restored["applied"] == [
        {
            "restored_location_id": root_id,
            "restored_ids": [root_id, child_id],
        }
    ]
    assert client.get(f"/locations/{root_id}").status_code == 200

    restore_undo = client.post("/assistant/undo").json()

    assert restore_undo["ok"] is True
    assert restore_undo["undone"][0]["rearchived_location"] == root_id
    assert client.get(f"/locations/{root_id}").status_code == 404


def test_reviewed_inventory_lifecycle_batches_are_atomic_and_undoable(tmp_path) -> None:
    client = product_client(tmp_path)
    box_id = client.post(
        "/locations", json={"type": "box", "label": "Retired projects"}
    ).json()["id"]
    first = client.post(
        f"/locations/{box_id}/items",
        json={"name": "Prototype bracket", "quantity": 2},
    ).json()
    second = client.post(
        f"/locations/{box_id}/items",
        json={"name": "Old controller", "quantity": 1},
    ).json()
    request = {
        "item_ids": [first["item_id"], second["item_id"]],
        "action": "archive",
    }

    preview = client.post("/inventory/lifecycle/preview", json=request).json()

    assert preview["item_count"] == 2
    assert preview["placement_count"] == 2
    assert preview["location_count"] == 1
    assert preview["preserves"] == [
        "placements",
        "photos",
        "history",
        "learned item home",
    ]
    assert len(client.get("/inventory").json()["items"]) == 2

    archived = client.post("/inventory/lifecycle/apply", json=request).json()

    assert archived["action"] == "manual_archive_inventory"
    assert archived["lifecycle_action"] == "archive"
    assert archived["interaction_id"] > 0
    assert client.get("/inventory").json()["items"] == []
    assert len(client.get("/inventory?archived=true").json()["items"]) == 2

    archive_undo = client.post("/assistant/undo").json()

    assert archive_undo["ok"] is True
    assert {
        entry["restored_archived_inventory_item"]
        for entry in archive_undo["undone"]
    } == {first["item_id"], second["item_id"]}
    assert len(client.get("/inventory").json()["items"]) == 2

    client.post("/inventory/lifecycle/apply", json=request)
    restore_request = {**request, "action": "restore"}
    restored = client.post("/inventory/lifecycle/apply", json=restore_request).json()

    assert restored["action"] == "manual_restore_inventory"
    assert len(client.get("/inventory").json()["items"]) == 2

    restore_undo = client.post("/assistant/undo").json()

    assert restore_undo["ok"] is True
    assert {
        entry["rearchived_inventory_item"] for entry in restore_undo["undone"]
    } == {first["item_id"], second["item_id"]}
    assert client.get("/inventory").json()["items"] == []


def test_single_item_lifecycle_routes_use_the_reviewed_undoable_contract(
    tmp_path,
) -> None:
    client = product_client(tmp_path)
    box_id = client.post(
        "/locations", json={"type": "box", "label": "Single item box"}
    ).json()["id"]
    item = client.post(
        f"/locations/{box_id}/items", json={"name": "Retired fixture"}
    ).json()

    archived = client.post(f"/inventory/{item['item_id']}/archive").json()

    assert archived["action"] == "manual_archive_inventory"
    assert archived["item_id"] == item["item_id"]
    assert archived["interaction_id"] > 0
    assert client.post("/assistant/undo").json()["undone"] == [
        {
            "restored_archived_inventory_item": item["item_id"],
            "item": "Retired fixture",
        }
    ]

    client.post(f"/inventory/{item['item_id']}/archive")
    restored = client.post(f"/inventory/{item['item_id']}/restore").json()

    assert restored["action"] == "manual_restore_inventory"
    assert client.post("/assistant/undo").json()["undone"] == [
        {
            "rearchived_inventory_item": item["item_id"],
            "item": "Retired fixture",
        }
    ]


def test_inventory_lifecycle_batch_restores_completed_changes_after_failure(
    tmp_path, monkeypatch
) -> None:
    original_archive = InventoryStore.archive_item
    calls = 0

    def fail_second_archive(self, item_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            return False
        return original_archive(self, item_id)

    monkeypatch.setattr(InventoryStore, "archive_item", fail_second_archive)
    client = product_client(tmp_path)
    box_id = client.post(
        "/locations", json={"type": "box", "label": "Project box"}
    ).json()["id"]
    first = client.post(
        f"/locations/{box_id}/items", json={"name": "First prototype"}
    ).json()
    second = client.post(
        f"/locations/{box_id}/items", json={"name": "Second prototype"}
    ).json()

    archived = client.post(
        "/inventory/lifecycle/apply",
        json={
            "item_ids": [first["item_id"], second["item_id"]],
            "action": "archive",
        },
    )

    assert archived.status_code == 409
    assert {
        item["item_id"] for item in client.get("/inventory").json()["items"]
    } == {first["item_id"], second["item_id"]}
    assert client.get("/inventory?archived=true").json()["items"] == []


def test_search_preserves_archived_path_and_reports_storage_state(tmp_path) -> None:
    client = product_client(tmp_path)
    root_id = client.post(
        "/locations", json={"type": "area", "label": "Attic"}
    ).json()["id"]
    tote_id = client.post(
        "/locations",
        json={"type": "tote", "label": "Winter tote", "parent_id": root_id},
    ).json()["id"]
    client.post(
        f"/locations/{tote_id}/items",
        json={"name": "Holiday lights", "quantity": 2},
    )
    client.post(f"/locations/{root_id}/archive")

    search_result = client.get("/search", params={"q": "holiday lights"}).json()[
        "results"
    ][0]
    assistant_result = client.post(
        "/assistant/find", json={"query": "holiday lights"}
    ).json()

    assert search_result["path"] == [
        {"id": root_id, "label": "Attic"},
        {"id": tote_id, "label": "Winter tote"},
    ]
    assert search_result["archived"] is True
    assert search_result["lit_location"] is None
    assert assistant_result["results"][0]["archived"] is True
    assert assistant_result["message"] == (
        "Holiday lights is in Attic > Winter tote in archived storage."
    )
    assert assistant_result["lit"] is False


def test_confirmed_agent_archive_can_be_undone(tmp_path) -> None:
    client = product_client(tmp_path)
    location_id = client.post(
        "/locations", json={"type": "area", "label": "Garage overflow"}
    ).json()["id"]

    confirmed = client.post(
        "/assistant/confirm",
        json={
            "command": "archive garage overflow",
            "plan": [
                {
                    "tool": "archive_location",
                    "arguments": {"location_id": location_id},
                }
            ],
        },
    )

    assert confirmed.status_code == 200
    assert client.get(f"/locations/{location_id}").status_code == 404
    undone = client.post("/assistant/undo").json()
    assert undone["ok"] is True
    assert undone["undone"][0]["restored_archived_location"] == location_id
    assert client.get(f"/locations/{location_id}").status_code == 200


def test_basic_item_move_works_without_an_external_agent(tmp_path) -> None:
    client = product_client(tmp_path)
    root_id = client.post(
        "/locations", json={"type": "room", "label": "Garage"}
    ).json()["id"]
    source_id = client.post(
        "/locations",
        json={
            "type": "cabinet",
            "label": "Printer Cabinet",
            "parent_id": root_id,
            "accepts_items": True,
        },
    ).json()["id"]
    destination_id = client.post(
        "/locations",
        json={"type": "shelf", "label": "Shelf 1", "parent_id": root_id},
    ).json()["id"]
    item_id = client.post(
        f"/locations/{source_id}/items",
        json={"name": "USB-C cable", "quantity": 1},
    ).json()["item_id"]

    proposed = client.post(
        "/assistant/chat",
        json={"text": "Move USB-C cable to Shelf 1"},
    ).json()

    assert proposed["action"] == "proposed"
    assert proposed["plan"] == [
        {
            "tool": "move_item",
            "arguments": {
                "item_id": item_id,
                "item": "USB-C cable",
                "from_location_id": source_id,
                "to_location_id": destination_id,
            },
        }
    ]
    confirmed = client.post(
        "/assistant/confirm",
        json={
            "plan": proposed["plan"],
            "command": "Move USB-C cable to Shelf 1",
            "source_interaction_id": proposed["interaction_id"],
        },
    ).json()
    assert confirmed["applied"][0]["moved_item"] == item_id
    assert client.get(f"/locations/{source_id}").json()["items"] == []
    assert client.get(f"/locations/{destination_id}").json()["items"][0]["name"] == "USB-C cable"


def test_basic_item_move_asks_when_item_has_multiple_source_locations(tmp_path) -> None:
    client = product_client(tmp_path)
    source_one = client.post(
        "/locations", json={"type": "box", "label": "Cable Box 1"}
    ).json()["id"]
    source_two = client.post(
        "/locations", json={"type": "box", "label": "Cable Box 2"}
    ).json()["id"]
    destination = client.post(
        "/locations", json={"type": "shelf", "label": "Shelf 1"}
    ).json()["id"]
    item_id = client.post(
        f"/locations/{source_one}/items",
        json={"name": "USB-C cable", "quantity": 1},
    ).json()["item_id"]
    client.post(
        f"/locations/{source_two}/items",
        json={"item_id": item_id, "name": "USB-C cable", "quantity": 2},
    )

    response = client.post(
        "/assistant/chat",
        json={"text": f"Move USB-C cable to {destination}"},
    ).json()

    assert response["decision"]["action"] == "clarify"
    assert "stored in more than one place" in response["message"]
    assert response["decision"]["plan"] == []


def test_container_runtime_is_parallel_and_hardware_neutral() -> None:
    config = json.loads((ROOT / "config/container.json").read_text(encoding="utf-8"))
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    locked_requirements = (
        ROOT / "requirements-container.lock"
    ).read_text(encoding="utf-8").splitlines()

    assert config["backend"] == "none"
    assert config["pixel_count"] == 0
    assert config["layout_path"] == "/app/config/storage-empty.json"
    assert config["database_path"] == "/data/inventory.db"
    assert config["edge"]["mode"] == "local"
    assert config["edge"]["target_device_id"] == ""
    assert (
        '"${MAGPIE_BIND_ADDRESS:-127.0.0.1}:${MAGPIE_HOST_PORT:-8766}:8765"'
        in compose
    )
    assert "magpie-data:/data" in compose
    assert "MAGPIE_CONFIG_FILE" in compose
    assert "/source/runtime.json:ro" in compose
    assert "LED_CONFIG: /runtime/runtime.json" in compose
    assert "magpie-runtime:/runtime:ro" in compose
    assert "/app/config/runtime.json:ro" not in compose
    assert "SUPABASE_PUBLISHABLE_KEY" in compose
    assert "SUPABASE_SERVICE_ROLE_KEY" in compose
    assert "MAGPIE_EDGE_TOKENS_JSON" in compose
    assert "BUILD_COMMIT" in compose
    assert "BUILD_INFO_PATH=/app/build.json" in dockerfile
    assert dockerfile.index("ARG BUILD_COMMIT") > dockerfile.index(
        "pip install --no-cache-dir --no-deps --no-build-isolation ."
    )
    assert dockerfile.index("ARG BUILD_COMMIT") < dockerfile.index(
        "RUN printf '{\"version\""
    )
    assert dockerfile.startswith(
        "FROM python:3.11.15-slim-bookworm@sha256:"
    )
    assert "COPY pyproject.toml README.md requirements-container.lock ./" in dockerfile
    assert "pip install --no-cache-dir -r requirements-container.lock" in dockerfile
    assert "pip install --no-cache-dir --no-deps --no-build-isolation ." in dockerfile
    assert "pip install --no-cache-dir --upgrade" not in dockerfile
    assert locked_requirements
    assert all(
        line and "==" in line and not line.startswith(("-", "#"))
        for line in locked_requirements
    )
    for dependency in ("fastapi", "httpx", "mcp", "qrcode", "uvicorn"):
        assert any(
            line.startswith(f"{dependency}==")
            for line in locked_requirements
        )
    assert ".env" in dockerignore
    assert ".env.*" in dockerignore
    assert "secrets.env" in dockerignore
    assert "config/container-hosted.json" in dockerignore
    assert (
        "HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5"
        in dockerfile
    )
    assert "http://127.0.0.1:8765/ready" in dockerfile
    assert 'CMD ["python", "-m", "searchable_storage_led.api"]' in dockerfile
    assert "init: true" in compose
    assert "read_only: true" in compose
    assert "/tmp:size=64m,mode=1777" in compose
    assert "cap_drop:" in compose
    assert "no-new-privileges:true" in compose
    assert "http://127.0.0.1:8765/ready" in compose


def test_local_container_repairs_persistent_volume_permissions_before_start() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "magpie-data-init:" in compose
    assert 'user: "0:0"' in compose
    assert "chown -R 10001:10001 /data" in compose
    assert (
        "install -o 10001 -g 10001 -m 0400 "
        "/source/runtime.json /runtime/runtime.json.next"
    ) in compose
    assert "mv -f /runtime/runtime.json.next /runtime/runtime.json" in compose
    assert (
        "${MAGPIE_CONFIG_FILE:-./config/container.json}:"
        "/source/runtime.json:ro"
    ) in compose
    assert "condition: service_completed_successfully" in compose


def test_hosted_container_example_is_authenticated_and_hardware_neutral() -> None:
    config_path = ROOT / "config/container-supabase.example.json"
    config = json.loads(
        config_path.read_text(encoding="utf-8")
    )
    compose = (ROOT / "compose.hosted.yaml").read_text(encoding="utf-8")

    assert config["backend"] == "none"
    assert config["pixel_count"] == 0
    assert config["layout_path"] == "/app/config/storage-layout.json"
    assert config["storage"]["backend"] == "supabase"
    assert config["auth"]["mode"] == "supabase"
    assert config["auth"]["publishable_key_env"] == "SUPABASE_PUBLISHABLE_KEY"
    assert config["auth"]["service_key_env"] == "SUPABASE_SERVICE_ROLE_KEY"
    assert config["auth"]["workspace_id"] == config["storage"]["workspace_id"]
    assert config["auth"]["multi_workspace"] is True
    assert config["auth"]["workspace_id"] == ""
    assert config["edge"]["mode"] == "workspace"
    assert config["edge"]["target_device_id"] == ""
    assert "MAGPIE_EDGE_TOKENS_JSON: ${MAGPIE_EDGE_TOKENS_JSON:-}" in compose
    assert 'magpie-preflight:' in compose
    assert 'profiles: ["verify"]' in compose
    assert 'entrypoint: ["searchable-storage-supabase-check"]' in compose
    assert 'command: ["--config", "/runtime/runtime.json"]' in compose
    assert "magpie-data-init:" in compose
    assert 'user: "0:0"' in compose
    assert "chown -R 10001:10001 /data" in compose
    assert (
        "install -o 10001 -g 10001 -m 0400 "
        "/source/runtime.json /runtime/runtime.json.next"
    ) in compose
    assert (
        "${MAGPIE_CONFIG_FILE:-./config/container-hosted.json}:"
        "/source/runtime.json:ro"
    ) in compose
    assert "magpie-runtime:/runtime:ro" in compose
    assert "LED_CONFIG: /runtime/runtime.json" in compose
    assert "/app/config/runtime.json:ro" not in compose
    assert "ghcr.io/jtalborough/searchable-storage:main" not in compose
    assert (
        "MAGPIE_IMAGE:?MAGPIE_IMAGE must be an immutable image reference"
        in compose
    )
    assert "condition: service_completed_successfully" in compose
    assert "init: true" in compose
    assert "http://127.0.0.1:8765/ready" in compose
    assert LedConfig.from_path(config_path).edge.mode == "workspace"


def test_hosted_edge_example_explicitly_enables_cabinet_bridge() -> None:
    config_path = ROOT / "config/container-supabase-edge.example.json"
    config = json.loads(
        config_path.read_text(encoding="utf-8")
    )

    assert config["backend"] == "none"
    assert config["pixel_count"] == 0
    assert config["layout_path"] == "/app/config/storage-empty.json"
    assert config["storage"]["backend"] == "supabase"
    assert config["auth"]["mode"] == "supabase"
    assert config["auth"]["workspace_id"] == config["storage"]["workspace_id"]
    assert config["edge"]["mode"] == "central"
    assert config["edge"]["target_device_id"] == "main-cabinet"
    assert LedConfig.from_path(config_path).edge.mode == "central"


def test_hardware_neutral_workspace_starts_without_cabinet_layout(tmp_path) -> None:
    client = product_client(tmp_path)

    locations = client.get("/locations").json()
    health = client.get("/health").json()
    capabilities = client.get("/capabilities").json()

    assert locations == {"roots": [], "locations": []}
    assert health["backend"] == "none"
    assert health["edge"]["enabled"] is False
    assert capabilities["features"]["lights"] is False
    assert capabilities["features"]["edge_command_queue"] is False
