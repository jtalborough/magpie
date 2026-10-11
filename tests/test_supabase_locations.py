from copy import deepcopy
import json
from uuid import uuid4

import pytest

from searchable_storage_led.locations import Location, LocationNameConflictError
from searchable_storage_led.repositories import (
    LOCATION_REPOSITORY_METHODS,
    missing_repository_methods,
)
from searchable_storage_led.supabase_repositories import SupabaseLocationRepository
from searchable_storage_led.supabase_client import HttpResponse, SupabaseError, SupabaseRestClient


class MemoryPostgrest:
    def __init__(self) -> None:
        self.select_calls = 0
        self.rpc_calls = []
        self.tables = {
            "locations": [],
            "hardware_bindings": [],
            "repository_events": [],
        }
        self.next_ids = {"hardware_bindings": 1, "repository_events": 1}

    def select(self, table, *, filters=None, order="", limit=None, **_kwargs):
        self.select_calls += 1
        rows = [deepcopy(row) for row in self.tables[table] if self._matches(row, filters)]
        if "desc" in order:
            key = order.split(".")[0]
            rows.sort(key=lambda row: row.get(key, 0), reverse=True)
        if limit is not None:
            rows = rows[:limit]
        return rows

    def insert(self, table, values, *, return_rows=True):
        row = deepcopy(values)
        row.setdefault("id", str(uuid4()))
        if table in self.next_ids:
            row.setdefault("local_id", self.next_ids[table])
            self.next_ids[table] += 1
        self.tables[table].append(row)
        return [deepcopy(row)] if return_rows else []

    def upsert(self, table, values, *, on_conflict, return_rows=True):
        keys = on_conflict.split(",")
        existing = next(
            (
                row
                for row in self.tables[table]
                if all(row.get(key) == values.get(key) for key in keys)
            ),
            None,
        )
        if existing is None:
            return self.insert(table, values, return_rows=return_rows)
        existing.update(deepcopy(values))
        return [deepcopy(existing)] if return_rows else []

    def update(self, table, values, *, filters, return_rows=True):
        rows = []
        for row in self.tables[table]:
            if self._matches(row, filters):
                row.update(deepcopy(values))
                rows.append(deepcopy(row))
        return rows if return_rows else []

    def delete(self, table, *, filters, return_rows=True):
        deleted = [row for row in self.tables[table] if self._matches(row, filters)]
        self.tables[table] = [
            row for row in self.tables[table] if not self._matches(row, filters)
        ]
        return deepcopy(deleted) if return_rows else []

    def rpc(self, function, arguments=None):
        arguments = deepcopy(arguments or {})
        self.rpc_calls.append((function, arguments))
        if function == "magpie_create_locations":
            return self._create_locations(arguments)
        if function == "magpie_set_hardware_bindings":
            return self._set_hardware_bindings(arguments)
        if function != "magpie_set_location_subtree_active":
            raise AssertionError(f"unexpected RPC: {function}")
        workspace_id = arguments["target_workspace_id"]
        root_code = arguments["root_location_code"]
        active = bool(arguments["target_active"])
        root = next(
            (
                row
                for row in self.tables["locations"]
                if row.get("workspace_id") == workspace_id
                and row.get("local_code") == root_code
            ),
            None,
        )
        if root is None:
            raise SupabaseError("unknown location")
        rows_by_parent = {}
        for row in self.tables["locations"]:
            rows_by_parent.setdefault(row.get("parent_id"), []).append(row)
        subtree = []
        queue = [root]
        archive_group = root.get("archive_group") or root["local_code"]
        while queue:
            row = queue.pop(0)
            subtree.append(row)
            queue.extend(
                sorted(
                    (
                        child
                        for child in rows_by_parent.get(row["id"], [])
                        if (
                            not active and child.get("active", True)
                        ) or (
                            active
                            and not child.get("active", True)
                            and (child.get("archive_group") or child["local_code"])
                            == archive_group
                        )
                    ),
                    key=lambda child: child["local_code"],
                )
            )
        if active and root.get("parent_id"):
            parent = next(
                row
                for row in self.tables["locations"]
                if row["id"] == root["parent_id"]
            )
            if not parent.get("active", True):
                raise SupabaseError("restore the parent location first")
        subtree_ids = {row["id"] for row in subtree}
        if active:
            names = set()
            for row in subtree:
                key = (row.get("parent_id"), " ".join(row["label"].lower().split()))
                if key in names:
                    raise SupabaseError(
                        "an active storage place with that name already exists at this level"
                    )
                names.add(key)
                if any(
                    sibling.get("active", True)
                    and sibling["id"] not in subtree_ids
                    and sibling.get("parent_id") == row.get("parent_id")
                    and " ".join(sibling["label"].lower().split()) == key[1]
                    for sibling in self.tables["locations"]
                ):
                    raise SupabaseError(
                        "an active storage place with that name already exists at this level"
                    )
        archived_at = None if active else "now"
        for row in subtree:
            row["active"] = active
            row["archived_at"] = archived_at
            row["archive_group"] = None if active else root_code
        return {"location_ids": [row["local_code"] for row in subtree]}

    def _create_locations(self, arguments):
        workspace_id = arguments["target_workspace_id"]
        staged = deepcopy(self.tables["locations"])
        created_codes = []
        for location in arguments["location_rows"]:
            code = location["local_code"]
            parent_code = location.get("parent_code")
            parent = next(
                (
                    row
                    for row in staged
                    if row.get("workspace_id") == workspace_id
                    and row.get("local_code") == parent_code
                    and row.get("active", True)
                ),
                None,
            ) if parent_code else None
            if parent_code and parent is None:
                raise SupabaseError(f"unknown or archived parent location: {parent_code}")
            normalized_label = " ".join(location["label"].lower().split())
            if any(
                row.get("workspace_id") == workspace_id
                and row.get("active", True)
                and row.get("parent_id") == (parent["id"] if parent else None)
                and " ".join(row["label"].lower().split()) == normalized_label
                for row in staged
            ):
                raise SupabaseError(
                    "an active storage place with that name already exists at this level"
                )
            staged.append(
                {
                    "id": str(uuid4()),
                    "workspace_id": workspace_id,
                    "local_code": code,
                    "parent_id": parent["id"] if parent else None,
                    "type": location["type"],
                    "label": location["label"],
                    "grid": location.get("grid", {}),
                    "cell_col": location.get("cell_col"),
                    "cell_row": location.get("cell_row"),
                    "cell_w": location.get("cell_w", 1),
                    "cell_h": location.get("cell_h", 1),
                    "light_target": location.get("light_target"),
                    "light_indexes": location.get("light_indexes", []),
                    "capacity_class": location.get("capacity_class", "medium"),
                    "purpose_tags": location.get("purpose_tags", []),
                    "placement_notes": location.get("placement_notes", ""),
                    "internal_dimensions": location.get("internal_dimensions", {}),
                    "accepts_items": location.get("accepts_items", True),
                    "active": True,
                    "archived_at": None,
                    "archive_group": None,
                }
            )
            created_codes.append(code)
        self.tables["locations"] = staged
        return {"location_ids": created_codes}

    def _set_hardware_bindings(self, arguments):
        workspace_id = arguments["target_workspace_id"]
        staged_bindings = deepcopy(self.tables["hardware_bindings"])
        staged_locations = deepcopy(self.tables["locations"])
        saved = []
        for binding in arguments["binding_rows"]:
            location = next(
                (
                    row
                    for row in staged_locations
                    if row.get("workspace_id") == workspace_id
                    and row.get("local_code") == binding["location_code"]
                    and row.get("active", True)
                ),
                None,
            )
            if location is None:
                raise SupabaseError(
                    f"unknown or archived location: {binding['location_code']}"
                )
            if binding.get("active", True):
                for current in staged_bindings:
                    if (
                        current.get("workspace_id") == workspace_id
                        and current.get("location_id") == location["id"]
                        and current.get("kind") == binding["kind"]
                        and current.get("controller_id") == binding["controller_id"]
                    ):
                        current["active"] = False
            existing = next(
                (
                    row
                    for row in staged_bindings
                    if row.get("workspace_id") == workspace_id
                    and row.get("location_id") == location["id"]
                    and row.get("kind") == binding["kind"]
                    and row.get("controller_id") == binding["controller_id"]
                    and row.get("address") == binding["address"]
                ),
                None,
            )
            if existing is None:
                existing = {
                    "id": str(uuid4()),
                    "local_id": self.next_ids["hardware_bindings"],
                    "workspace_id": workspace_id,
                    "location_id": location["id"],
                }
                self.next_ids["hardware_bindings"] += 1
                staged_bindings.append(existing)
            existing.update(
                {
                    "kind": binding["kind"],
                    "controller_id": binding["controller_id"],
                    "address": binding["address"],
                    "metadata": deepcopy(binding.get("metadata", {})),
                    "active": binding.get("active", True),
                }
            )
            if binding["kind"] == "wled_pixels":
                location["light_target"] = (
                    binding["controller_id"] if binding.get("active", True) else None
                )
                location["light_indexes"] = (
                    list(binding.get("metadata", {}).get("pixels", []))
                    if binding.get("active", True)
                    else []
                )
            saved.append(deepcopy(existing) | {"location_code": binding["location_code"]})
        self.tables["hardware_bindings"] = staged_bindings
        original_locations = {row["id"]: row for row in self.tables["locations"]}
        for staged in staged_locations:
            original = original_locations[staged["id"]]
            original.clear()
            original.update(staged)
        return {"bindings": saved}

    @staticmethod
    def _matches(row, filters):
        for key, expression in (filters or {}).items():
            operator, expected = expression.split(".", 1)
            if operator == "eq" and str(row.get(key)) != expected:
                return False
            if operator == "neq" and str(row.get(key)) == expected:
                return False
        return True


def _repository():
    client = MemoryPostgrest()
    return client, SupabaseLocationRepository(client, workspace_id="workspace-1")


def test_supabase_location_repository_satisfies_contract() -> None:
    client, repository = _repository()

    assert missing_repository_methods(repository, LOCATION_REPOSITORY_METHODS) == []
    repository.health_probe()
    assert client.select_calls == 1


def test_supabase_location_events_support_targeted_workspace_evidence() -> None:
    client, repository = _repository()
    repository.upsert(Location(id="attic", type="area"))
    repository.record_event(
        event_type="workspace_visual_multi_root_setup",
        location_id="attic",
        note="two roots",
    )

    assert repository.has_event("workspace_visual_multi_root_setup") is True
    assert (
        repository.has_event(
            "workspace_visual_multi_root_setup",
            location_id="attic",
        )
        is True
    )
    assert repository.has_event("workspace_cross_root_item_move") is False
    assert repository.has_event("") is False


def test_latest_location_event_uses_one_bounded_workspace_query() -> None:
    client, repository = _repository()
    repository.upsert(Location(id="attic", type="area"))
    repository.record_event(
        event_type="location_contents_verified",
        location_id="attic",
        quantity=1,
        note="older",
    )
    repository.record_event(
        event_type="location_contents_verified",
        location_id="attic",
        quantity=2,
        note="newer",
    )
    calls = []
    original_select = client.select

    def capture_select(table, **kwargs):
        calls.append((table, kwargs))
        return original_select(table, **kwargs)

    client.select = capture_select

    event = repository.latest_event(
        "location_contents_verified",
        location_id="attic",
    )

    assert event is not None
    assert event["quantity"] == 2
    assert event["note"] == "newer"
    assert len(calls) == 1
    table, kwargs = calls[0]
    assert table == "repository_events"
    assert kwargs["order"] == "local_id.desc"
    assert kwargs["limit"] == 1
    assert kwargs["filters"] == {
        "workspace_id": "eq.workspace-1",
        "event_type": "eq.location_contents_verified",
        "location_code": "eq.attic",
    }


def test_latest_location_events_bulk_read_is_bounded_and_reports_truncation() -> None:
    client, repository = _repository()
    for location_id in ("attic", "garage"):
        repository.upsert(Location(id=location_id, type="area"))
        repository.record_event(
            event_type="location_contents_verified",
            location_id=location_id,
            quantity=1,
            note=location_id,
        )
    repository.record_event(
        event_type="location_contents_verified",
        location_id="attic",
        quantity=2,
        note="attic newest",
    )
    calls = []
    original_select = client.select

    def capture_select(table, **kwargs):
        calls.append((table, kwargs))
        return original_select(table, **kwargs)

    client.select = capture_select

    events, truncated = repository.latest_events_by_location(
        "location_contents_verified",
        limit=1,
    )

    assert truncated is True
    assert events == {
        "attic": {
            "id": 3,
            "event_type": "location_contents_verified",
            "item_id": None,
            "location_id": "attic",
            "quantity": 2.0,
            "note": "attic newest",
            "created_at": None,
        }
    }
    assert calls == [
        (
            "repository_events",
            {
                "columns": "local_id,event_type,item_local_id,location_code,quantity,note,created_at",
                "filters": {
                    "workspace_id": "eq.workspace-1",
                    "event_type": "eq.location_contents_verified",
                },
                "order": "local_id.desc",
                "limit": 2,
            },
        )
    ]


def test_live_latest_location_events_uses_latest_per_place_rpc() -> None:
    requests = []

    def transport(request, timeout):
        requests.append((request, timeout))
        return HttpResponse(
            200,
            json.dumps(
                [
                    {
                        "local_id": 9,
                        "event_type": "location_contents_verified",
                        "item_local_id": None,
                        "location_code": "attic",
                        "quantity": 3,
                        "note": "three records",
                        "created_at": "2026-08-01T12:00:00+00:00",
                    }
                ]
            ).encode(),
        )

    client = SupabaseRestClient(
        url="https://project.supabase.co",
        api_key="secret",
        transport=transport,
    )
    repository = SupabaseLocationRepository(client, workspace_id="workspace-1")

    events, truncated = repository.latest_events_by_location(
        "location_contents_verified"
    )

    assert truncated is False
    assert events["attic"]["id"] == 9
    assert events["attic"]["quantity"] == 3
    assert len(requests) == 1
    request, _ = requests[0]
    assert request.full_url.endswith("/rest/v1/rpc/magpie_latest_location_events")
    assert json.loads(request.data) == {
        "target_workspace_id": "workspace-1",
        "target_event_type": "location_contents_verified",
    }


def test_location_cache_can_be_invalidated_after_cross_repository_write() -> None:
    client, repository = _repository()
    assert repository.get("new-bin") is None
    client.tables["locations"].append(
        {
            "id": "location-new-bin",
            "workspace_id": "workspace-1",
            "local_code": "new-bin",
            "parent_id": None,
            "type": "bin",
            "label": "New bin",
            "grid": {},
            "cell_col": None,
            "cell_row": None,
            "cell_w": 1,
            "cell_h": 1,
            "light_target": None,
            "light_indexes": [],
            "capacity_class": "medium",
            "purpose_tags": [],
            "placement_notes": "",
            "internal_dimensions": {},
            "accepts_items": True,
            "active": True,
            "archived_at": None,
        }
    )

    assert repository.get("new-bin") is None
    repository.invalidate_cache()
    assert repository.get("new-bin").label == "New bin"


def test_location_name_guard_is_translated_to_domain_conflict(monkeypatch) -> None:
    client, repository = _repository()

    def reject_duplicate(*_args, **_kwargs):
        raise SupabaseError(
            "Supabase POST locations failed (409): "
            "an active storage place with that name already exists at this level"
        )

    monkeypatch.setattr(client, "upsert", reject_duplicate)

    with pytest.raises(LocationNameConflictError, match="already exists"):
        repository.upsert(Location(id="duplicate", type="bin", label="Hardware"))


def test_create_many_builds_parent_and_children_with_one_rpc() -> None:
    client, repository = _repository()

    created = repository.create_many(
        [
            Location(id="garage", type="room", label="Garage"),
            Location(
                id="parts-cabinet",
                type="cabinet",
                parent_id="garage",
                label="Parts cabinet",
            ),
            Location(
                id="small-parts",
                type="drawer",
                parent_id="parts-cabinet",
                label="Small parts",
            ),
        ]
    )

    assert created == ["garage", "parts-cabinet", "small-parts"]
    assert [call[0] for call in client.rpc_calls] == ["magpie_create_locations"]
    assert repository.get("parts-cabinet").parent_id == "garage"
    assert repository.get("small-parts").parent_id == "parts-cabinet"


def test_create_many_name_conflict_creates_nothing() -> None:
    client, repository = _repository()
    repository.upsert(Location(id="garage", type="room", label="Garage"))
    rows_before = deepcopy(client.tables["locations"])

    with pytest.raises(LocationNameConflictError, match="already exists"):
        repository.create_many(
            [
                Location(
                    id="cabinet",
                    type="cabinet",
                    parent_id="garage",
                    label="Parts",
                ),
                Location(
                    id="duplicate-cabinet",
                    type="cabinet",
                    parent_id="garage",
                    label="  PARTS  ",
                ),
            ]
        )

    assert client.tables["locations"] == rows_before


def test_location_tree_round_trip_move_and_cycle_protection() -> None:
    _, repository = _repository()
    repository.upsert(
        Location(
            id="cabinet",
            type="cabinet",
            label="Main cabinet",
            grid={"cols": 2, "rows": 1},
            light_target="wled-main",
            light_indexes=[0, 1],
            internal_dimensions={"width_mm": 200, "depth_mm": 140, "height_mm": 40},
            accepts_items=False,
        )
    )
    repository.upsert(
        Location(
            id="B001",
            type="bin",
            parent_id="cabinet",
            label="Blue bin",
            col=0,
            row=0,
            capacity_class="small",
            purpose_tags=["hardware"],
        )
    )
    repository.upsert(Location(id="pouch", type="pouch", parent_id="B001"))

    assert repository.count() == 3
    assert repository.get("cabinet").internal_dimensions == {
        "width_mm": 200.0,
        "depth_mm": 140.0,
        "height_mm": 40.0,
    }
    assert repository.get("cabinet").accepts_items is False
    assert [loc.id for loc in repository.children("cabinet")] == ["B001"]
    assert [loc.id for loc in repository.ancestors("pouch")] == [
        "pouch",
        "B001",
        "cabinet",
    ]
    assert repository.nearest_lit("pouch").id == "cabinet"
    assert repository.get("B001").purpose_tags == ["hardware"]

    moved = repository.move("pouch", "cabinet")
    assert moved.parent_id == "cabinet"
    with pytest.raises(ValueError, match="own subtree"):
        repository.move("cabinet", "pouch")


def test_location_upsert_preserves_graph_invariants() -> None:
    _, repository = _repository()
    repository.upsert(Location(id="garage", type="room"))
    repository.upsert(Location(id="cabinet", type="cabinet", parent_id="garage"))
    repository.upsert(Location(id="drawer", type="drawer", parent_id="cabinet"))

    with pytest.raises(KeyError, match="unknown parent"):
        repository.upsert(Location(id="lost-box", type="box", parent_id="missing"))
    with pytest.raises(ValueError, match="own parent"):
        repository.upsert(Location(id="self", type="box", parent_id="self"))
    with pytest.raises(ValueError, match="own subtree"):
        repository.upsert(Location(id="garage", type="room", parent_id="drawer"))

    assert repository.get("garage").parent_id is None


def test_location_upsert_only_allows_archived_children_under_archived_parents() -> None:
    _, repository = _repository()
    repository.upsert(
        Location(id="attic", type="area", active=False, archived_at="now")
    )

    with pytest.raises(ValueError, match="parent location is archived"):
        repository.upsert(Location(id="active-box", type="box", parent_id="attic"))

    repository.upsert(
        Location(
            id="archived-box",
            type="box",
            parent_id="attic",
            active=False,
            archived_at="now",
        )
    )
    assert repository.get_any("archived-box").parent_id == "attic"


def test_location_reader_ignores_malformed_internal_dimensions() -> None:
    client, repository = _repository()
    client.tables["locations"].append(
        {
            "id": str(uuid4()),
            "workspace_id": "workspace-1",
            "local_code": "B001",
            "type": "bin",
            "label": "Bin 1",
            "internal_dimensions": {
                "width_mm": "42",
                "depth_mm": "unknown",
                "height_mm": -1,
            },
        }
    )

    assert repository.get("B001").internal_dimensions == {"width_mm": 42.0}


def test_location_history_and_hardware_binding_round_trip() -> None:
    client, repository = _repository()
    repository.upsert(Location(id="cabinet", type="cabinet"))
    repository.record_event(
        event_type="location_updated",
        location_id="cabinet",
        note="renamed",
        quantity=2,
    )

    events = repository.events("cabinet")
    assert events[0] | {"created_at": None} == {
        "id": 1,
        "event_type": "location_updated",
        "item_id": None,
        "location_id": "cabinet",
        "quantity": 2.0,
        "note": "renamed",
        "created_at": None,
    }

    binding = repository.set_hardware_binding(
        location_id="cabinet",
        kind="wled-pixels",
        controller_id="main",
        address="4,5,6",
        metadata={"pixels": [4, 5, 6]},
    )
    assert binding.id == 1
    assert repository.hardware_bindings("cabinet") == [binding]
    remote_location = client.tables["locations"][0]
    assert remote_location["light_target"] == "main"
    assert remote_location["light_indexes"] == [4, 5, 6]

    replacement = repository.set_hardware_binding(
        location_id="cabinet",
        kind="wled_pixels",
        controller_id="main",
        address="7,8",
        metadata={"pixels": [7, 8]},
    )
    bindings = repository.hardware_bindings("cabinet")
    assert replacement.active is True
    assert [(entry.address, entry.active) for entry in bindings] == [
        ("7,8", True),
        ("4,5,6", False),
    ]
    assert remote_location["light_indexes"] == [7, 8]

    calls_before_bulk = client.select_calls
    grouped = repository.hardware_bindings_by_location(["cabinet", "missing"])
    assert [(entry.address, entry.active) for entry in grouped["cabinet"]] == [
        ("7,8", True),
        ("4,5,6", False),
    ]
    assert client.select_calls == calls_before_bulk + 1


def test_hardware_binding_batch_uses_one_atomic_rpc() -> None:
    client, repository = _repository()
    repository.create_many(
        [
            Location(id="cabinet", type="cabinet", accepts_items=False),
            Location(id="B001", type="bin", parent_id="cabinet"),
            Location(id="B002", type="bin", parent_id="cabinet"),
        ]
    )

    bindings = repository.set_hardware_bindings(
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
                "address": "1,2",
                "metadata": {"pixels": [1, 2]},
            },
        ]
    )

    assert [binding.location_id for binding in bindings] == ["B001", "B002"]
    assert [binding.address for binding in bindings] == ["0", "1,2"]
    assert client.rpc_calls[-1][0] == "magpie_set_hardware_bindings"
    assert repository.get("B002").light_indexes == [1, 2]


def test_delete_subtree_removes_descendants() -> None:
    _, repository = _repository()
    repository.upsert(Location(id="root", type="room"))
    repository.upsert(Location(id="box", type="box", parent_id="root"))
    repository.upsert(Location(id="pouch", type="pouch", parent_id="box"))

    assert repository.delete_subtree("box") == ["box", "pouch"]
    assert [location.id for location in repository.all()] == ["root"]


def test_archive_and_restore_subtree_updates_remote_state() -> None:
    client, repository = _repository()
    repository.upsert(Location(id="attic", type="area"))
    repository.upsert(Location(id="tote", type="tote", parent_id="attic"))

    assert repository.archive_subtree("attic") == ["attic", "tote"]
    assert repository.all() == []
    assert repository.get_any("attic").archived_at
    assert {row["active"] for row in client.tables["locations"]} == {False}

    assert repository.restore_subtree("attic") == ["attic", "tote"]
    assert [location.id for location in repository.all()] == ["attic", "tote"]
    assert all(row["archived_at"] is None for row in client.tables["locations"])
    assert [call[1]["target_active"] for call in client.rpc_calls] == [False, True]


def test_restore_parent_preserves_separately_archived_descendant() -> None:
    client, repository = _repository()
    repository.upsert(Location(id="attic", type="area"))
    repository.upsert(Location(id="retired", type="box", parent_id="attic"))
    repository.upsert(Location(id="current", type="box", parent_id="attic"))

    assert repository.archive_subtree("retired") == ["retired"]
    assert repository.archive_subtree("attic") == ["attic", "current"]
    assert repository.restore_subtree("attic") == ["attic", "current"]

    assert repository.get("current") is not None
    assert repository.get("retired") is None
    retired = repository.get_any("retired")
    assert retired is not None
    assert retired.archive_group == "retired"
    retired_row = next(
        row for row in client.tables["locations"] if row["local_code"] == "retired"
    )
    assert retired_row["active"] is False


def test_restore_subtree_rejects_name_conflicts_without_partial_changes() -> None:
    client, repository = _repository()
    repository.upsert(Location(id="attic", type="area", label="Attic"))
    repository.upsert(
        Location(id="parts", type="bin", parent_id="attic", label="Parts")
    )
    repository.archive_subtree("parts")
    repository.upsert(
        Location(
            id="replacement-parts",
            type="bin",
            parent_id="attic",
            label="  PARTS  ",
        )
    )

    with pytest.raises(LocationNameConflictError, match="already exists"):
        repository.restore_subtree("parts")

    assert repository.get_any("parts").active is False
    assert repository.get("replacement-parts").active is True
