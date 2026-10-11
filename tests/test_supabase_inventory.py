from copy import deepcopy
import json
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from searchable_storage_led.api import create_app
from searchable_storage_led.config import LedConfig
from searchable_storage_led.locations import Location
from searchable_storage_led.repositories import StorageRepositories
from searchable_storage_led.supabase_repositories import (
    SupabaseInventoryRepository,
    SupabaseLocationRepository,
)
from searchable_storage_led.supabase_client import HttpResponse, SupabaseRestClient


class MemoryPostgrest:
    def __init__(self) -> None:
        self.select_calls = 0
        self.select_history: list[dict[str, object]] = []
        self.tables = {
            "items": [],
            "item_aliases": [],
            "inventory_entries": [],
            "locations": [],
            "photos": [],
            "inventory_review_photos": [],
            "repository_events": [],
            "placement_rules": [],
            "size_rules": [],
            "agent_examples": [],
            "assistant_interactions": [],
            "active_guidance_states": [],
            "voice_attempts": [],
            "hardware_bindings": [],
        }
        self.next_ids = {
            "items": 1,
            "photos": 1,
            "repository_events": 1,
            "placement_rules": 1,
            "size_rules": 1,
            "agent_examples": 1,
            "assistant_interactions": 1,
            "voice_attempts": 1,
        }
        self.objects = {}

    def add_location(self, code: str) -> dict[str, object]:
        row = {
            "id": str(uuid4()),
            "workspace_id": "workspace-1",
            "local_code": code,
            "type": "bin",
            "label": code,
            "grid": {},
            "light_indexes": [],
            "capacity_class": "medium",
            "purpose_tags": [],
        }
        self.tables["locations"].append(row)
        return row

    def select(self, table, *, filters=None, order="", limit=None, **_kwargs):
        self.select_calls += 1
        self.select_history.append(
            {
                "table": table,
                "filters": deepcopy(filters or {}),
                "order": order,
                "limit": limit,
            }
        )
        effective_filters = dict(filters or {})
        if table == "inventory_entries":
            effective_filters.pop("items.active", None)
        rows = [
            deepcopy(row)
            for row in self.tables[table]
            if self._matches(row, effective_filters)
        ]
        if order:
            field, direction = order.split(",")[0].split(".")[:2]
            rows.sort(key=lambda row: row.get(field, 0), reverse=direction == "desc")
        return rows[:limit] if limit is not None else rows

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
        if function == "magpie_merge_items":
            args = arguments or {}
            workspace_id = args["target_workspace_id"]
            source = next(
                row
                for row in self.tables["items"]
                if row["workspace_id"] == workspace_id
                and row["local_id"] == args["source_item_local_id"]
            )
            target = next(
                row
                for row in self.tables["items"]
                if row["workspace_id"] == workspace_id
                and row["local_id"] == args["canonical_item_local_id"]
            )
            source_entries = [
                row
                for row in self.tables["inventory_entries"]
                if row["workspace_id"] == workspace_id
                and row["item_id"] == source["id"]
            ]
            for source_entry in source_entries:
                target_entry = next(
                    (
                        row
                        for row in self.tables["inventory_entries"]
                        if row["workspace_id"] == workspace_id
                        and row["item_id"] == target["id"]
                        and row["location_id"] == source_entry["location_id"]
                    ),
                    None,
                )
                if target_entry is None:
                    source_entry["item_id"] = target["id"]
                    continue
                target_entry["quantity"] = float(target_entry["quantity"]) + float(
                    source_entry["quantity"]
                )
                target_entry["needs_review"] = bool(
                    target_entry.get("needs_review")
                    or source_entry.get("needs_review")
                )
                for link in list(self.tables["inventory_review_photos"]):
                    if link["inventory_entry_id"] == source_entry["id"]:
                        self.upsert(
                            "inventory_review_photos",
                            {
                                "workspace_id": workspace_id,
                                "inventory_entry_id": target_entry["id"],
                                "photo_id": link["photo_id"],
                            },
                            on_conflict="inventory_entry_id,photo_id",
                            return_rows=False,
                        )
                self.tables["inventory_review_photos"] = [
                    link
                    for link in self.tables["inventory_review_photos"]
                    if link["inventory_entry_id"] != source_entry["id"]
                ]
                self.tables["inventory_entries"].remove(source_entry)
            for photo in self.tables["photos"]:
                if photo.get("item_id") == source["id"]:
                    photo["item_id"] = target["id"]
                if photo.get("object_type") == "item" and photo.get("object_id") == source["id"]:
                    photo["object_id"] = target["id"]
            aliases = [
                source["name"],
                *[
                    row["alias"]
                    for row in self.tables["item_aliases"]
                    if row["item_id"] == source["id"]
                ],
            ]
            self.tables["item_aliases"] = [
                row for row in self.tables["item_aliases"] if row["item_id"] != source["id"]
            ]
            for alias in aliases:
                self.upsert(
                    "item_aliases",
                    {"item_id": target["id"], "alias": alias},
                    on_conflict="item_id,alias",
                    return_rows=False,
                )
            for event in self.tables["repository_events"]:
                if event.get("item_id") == source["id"]:
                    event["item_id"] = target["id"]
                    event["item_local_id"] = target["local_id"]
            source.update(
                {
                    "active": False,
                    "archived_at": "now",
                    "merged_into_item_id": target["id"],
                }
            )
            self.insert(
                "repository_events",
                {
                    "workspace_id": workspace_id,
                    "event_type": "item_merged",
                    "item_id": target["id"],
                    "item_local_id": target["local_id"],
                    "note": f"{source['name']} merged into {target['name']}",
                },
                return_rows=False,
            )
            return {
                "merged": True,
                "source_item_id": source["local_id"],
                "canonical_item_id": target["local_id"],
            }
        if function != "magpie_put_item_with_photos":
            raise ValueError(f"unknown RPC: {function}")
        args = arguments or {}
        workspace_id = args["target_workspace_id"]
        photo_ids = list(dict.fromkeys(args["target_photo_local_ids"]))
        photos = [
            row
            for row in self.tables["photos"]
            if row["workspace_id"] == workspace_id and row["local_id"] in photo_ids
        ]
        if len(photos) != len(photo_ids):
            raise ValueError("one or more photos were not found")
        location = next(
            (
                row
                for row in self.tables["locations"]
                if row["workspace_id"] == workspace_id
                and row["local_code"].upper() == args["target_location_code"].upper()
            ),
            None,
        )
        if location is None:
            raise ValueError(f"location {args['target_location_code']} not found")
        item = next(
            (
                row
                for row in self.tables["items"]
                if row["workspace_id"] == workspace_id
                and row["name"] == args["target_item_name"].strip()
            ),
            None,
        )
        if item is None:
            item = self.insert(
                "items",
                {
                    "workspace_id": workspace_id,
                    "name": args["target_item_name"].strip(),
                    "description": "",
                    "category": "",
                },
            )[0]
        entry = next(
            (
                row
                for row in self.tables["inventory_entries"]
                if row["workspace_id"] == workspace_id
                and row["item_id"] == item["id"]
                and row["location_id"] == location["id"]
            ),
            None,
        )
        previous_quantity = entry.get("quantity") if entry else None
        previous_unit = entry.get("unit") if entry else None
        quantity = float(args["requested_quantity"])
        if args["quantity_mode"] == "add" and previous_quantity is not None:
            quantity += float(previous_quantity)
        values = {
            "workspace_id": workspace_id,
            "item_id": item["id"],
            "location_id": location["id"],
            "quantity": quantity,
            "unit": args["requested_unit"],
            "status": "review" if args["mark_for_review"] else "present",
            "needs_review": args["mark_for_review"],
            "review_reason": args["target_review_reason"] if args["mark_for_review"] else "",
        }
        if entry is None:
            entry = self.insert("inventory_entries", values)[0]
        else:
            entry.update(values)
        item["last_seen_location_id"] = location["id"]
        item.setdefault("home_location_id", location["id"])
        for photo in photos:
            photo.update(
                {
                    "item_id": item["id"],
                    "object_type": "item",
                    "object_id": item["id"],
                }
            )
            if args["mark_for_review"]:
                self.upsert(
                    "inventory_review_photos",
                    {
                        "workspace_id": workspace_id,
                        "inventory_entry_id": entry["id"],
                        "photo_id": photo["id"],
                    },
                    on_conflict="inventory_entry_id,photo_id",
                    return_rows=False,
                )
        self.insert(
            "repository_events",
            {
                "workspace_id": workspace_id,
                "event_type": "inventory_put",
                "item_id": item["id"],
                "item_local_id": item["local_id"],
                "location_id": location["id"],
                "location_code": location["local_code"],
                "quantity": quantity,
                "note": "needs_review" if args["mark_for_review"] else "",
            },
            return_rows=False,
        )
        return {
            "stored": item["name"],
            "item_id": item["local_id"],
            "location_id": location["local_code"],
            "needs_review": args["mark_for_review"],
            "previous_quantity": previous_quantity,
            "previous_unit": previous_unit,
            "quantity": quantity,
            "photo_ids": photo_ids,
        }

    def public_object_url(self, *, bucket: str, path: str) -> str:
        return f"https://storage.test/{bucket}/{path}"

    def upload_object(self, *, bucket, path, data, content_type, **_kwargs):
        self.objects[(bucket, path)] = (data, content_type)
        return {"Key": f"{bucket}/{path}"}

    def delete_objects(self, *, bucket, paths):
        for path in paths:
            self.objects.pop((bucket, path), None)

    def download_object(self, *, bucket, path):
        return self.objects[(bucket, path)][0]

    @staticmethod
    def _matches(row, filters):
        for key, expression in (filters or {}).items():
            if key == "or":
                clauses = expression.strip("()").split(",")
                if not any(
                    (
                        str(row.get(field) or "").lower() == expected
                        if operator == "eq"
                        else str(row.get(field) or "") != expected
                    )
                    for field, operator, expected in (
                        clause.split(".", 2) for clause in clauses
                    )
                    if operator in {"eq", "neq"}
                ):
                    return False
                continue
            operator, expected = expression.split(".", 1)
            actual = row.get(key)
            if "->>" in key:
                column, field = key.split("->>", 1)
                document = row.get(column)
                actual = document.get(field) if isinstance(document, dict) else None
            if operator == "eq":
                if isinstance(actual, bool):
                    actual = str(actual).lower()
                if str(actual) != expected:
                    return False
            elif operator == "neq" and str(actual) == expected:
                return False
            elif operator == "in":
                values = [value.strip('"') for value in expected.strip("()").split(",")]
                if str(actual) not in values:
                    return False
            elif operator == "gt" and float(actual or 0) <= float(expected):
                return False
        return True


def _repository():
    client = MemoryPostgrest()
    client.add_location("B001")
    client.add_location("B002")
    return client, SupabaseInventoryRepository(client, workspace_id="workspace-1")


def test_item_inventory_search_alias_and_metadata_round_trip() -> None:
    _, repository = _repository()
    item_id = repository.upsert_item(
        name="M4 T-nuts",
        description="Hammer-in threaded inserts",
        category="hardware",
        aliases=["tee nuts"],
    )
    repository.put_inventory(
        item_id=item_id,
        bin_id="B1",
        quantity=24,
        unit="pieces",
    )

    result = repository.search("hammer threaded")[0]
    assert result.item_id == item_id
    assert result.name == "M4 T-nuts"
    assert result.aliases == ["tee nuts"]
    assert result.bin_id == "B001"
    assert result.quantity == 24
    assert result.home_location_id == "B001"
    assert repository.exact_match("tee nuts") == [result]
    assert repository.suggest_home("M4 T-nuts") == "B001"

    repository.update_item(
        item_id=item_id,
        name="M4 pronged T-nuts",
        category="fasteners",
        aliases=["four prong nuts"],
    )
    updated = repository.item_inventory(item_id)[0]
    assert updated.name == "M4 pronged T-nuts"
    assert updated.category == "fasteners"
    assert updated.aliases == ["four prong nuts"]


def test_review_move_merge_remove_and_history() -> None:
    _, repository = _repository()
    item_id = repository.upsert_item(name="M4 T-nuts")
    repository.put_inventory(
        item_id=item_id,
        bin_id="B001",
        quantity=3,
        unit="each",
        needs_review=True,
        review_reason="vision uncertain",
    )
    assert repository.review_queue()[0].review_reason == "vision uncertain"
    assert repository.location_review_counts() == {"B001": 1}

    repository.mark_reviewed(item_id=item_id, bin_id="B001")
    assert repository.review_queue() == []
    assert repository.location_review_counts() == {}
    repository.put_inventory(item_id=item_id, bin_id="B002", quantity=2, unit="each")
    assert repository.move_inventory(
        item_id=item_id,
        from_bin="B001",
        to_bin="B002",
    )
    assert repository.bin_inventory("B002")[0].quantity == 5
    assert repository.location_item_counts() == {"B002": 1}
    assert repository.occupied_bin_ids() == {"B002"}

    event_types = [event["event_type"] for event in repository.item_events(item_id)]
    assert "inventory_put" in event_types
    assert "inventory_move" in event_types
    assert repository.remove_inventory(
        item_id=item_id,
        bin_id="B002",
        note="resolved missing placement",
    )
    assert repository.item_events(item_id)[0]["note"] == "resolved missing placement"
    assert repository.list_inventory() == []


def test_put_inventory_invalidates_a_cache_primed_before_the_write() -> None:
    _, repository = _repository()

    first_id = repository.upsert_item(name="M3 T-nuts")
    assert repository.item_inventory(first_id) == []
    repository.put_inventory(
        item_id=first_id,
        bin_id="B001",
        quantity=10,
        unit="pieces",
    )

    second_id = repository.upsert_item(name="M4 T-nuts")
    assert repository.item_inventory(second_id) == []
    repository.put_inventory(
        item_id=second_id,
        bin_id="B001",
        quantity=10,
        unit="pieces",
    )

    assert {
        (item.name, item.bin_id, item.home_location_id)
        for item in repository.list_inventory()
    } == {
        ("M3 T-nuts", "B001", "B001"),
        ("M4 T-nuts", "B001", "B001"),
    }


def test_review_approval_preserves_dynamic_location_code() -> None:
    client, repository = _repository()
    client.add_location("attic-parts-box")
    item_id = repository.upsert_item(name="Unsorted brackets")
    repository.put_inventory(
        item_id=item_id,
        bin_id="attic-parts-box",
        quantity=1,
        unit="each",
        needs_review=True,
        review_reason="Confirm bracket type.",
        normalize_bin=False,
    )

    repository.mark_reviewed(item_id=item_id, bin_id="attic-parts-box")

    assert repository.review_queue() == []
    [entry] = client.tables["inventory_entries"]
    assert entry["needs_review"] is False
    assert entry["status"] == "present"


def test_inventory_helpers_preserve_dynamic_location_codes() -> None:
    client, repository = _repository()
    client.add_location("attic-parts-box")
    client.add_location("garage-parts-box")
    item_id = repository.upsert_item(name="Assorted brackets")

    repository.put_inventory(
        item_id=item_id,
        bin_id="attic-parts-box",
        quantity=3,
        unit="each",
    )

    [stored] = repository.bin_inventory("attic-parts-box")
    assert stored.bin_id == "attic-parts-box"
    assert repository.move_inventory(
        item_id=item_id,
        from_bin="attic-parts-box",
        to_bin="garage-parts-box",
        normalize_bin=True,
    )
    [moved] = repository.bin_inventory("garage-parts-box")
    assert moved.bin_id == "garage-parts-box"
    assert repository.clear_bin_inventory("garage-parts-box") == 1
    assert repository.list_inventory() == []


def test_inventory_snapshot_is_cached_until_a_write() -> None:
    client, repository = _repository()
    item_id = repository.upsert_item(name="M4 T-nuts")
    repository.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")

    assert repository.list_inventory()[0].quantity == 1
    calls_after_first_read = client.select_calls
    assert repository.list_inventory()[0].quantity == 1
    assert client.select_calls == calls_after_first_read

    repository.put_inventory(item_id=item_id, bin_id="B001", quantity=2, unit="each")
    assert repository.list_inventory()[0].quantity == 2
    assert client.select_calls > calls_after_first_read


def test_inventory_snapshot_uses_one_joined_supabase_read() -> None:
    class JoinedInventoryClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, str], str]] = []

        def select(
            self,
            table: str,
            *,
            columns: str = "*",
            filters: dict[str, str] | None = None,
            order: str = "",
            **_kwargs,
        ) -> list[dict[str, object]]:
            self.calls.append((table, columns, filters or {}, order))
            return [
                {
                    "id": "entry-1",
                    "item_id": "item-1",
                    "location_id": "location-1",
                    "quantity": 4,
                    "unit": "each",
                    "needs_review": True,
                    "review_reason": "Confirm size",
                    "items": {
                        "local_id": 7,
                        "name": "M4 T-nuts",
                        "description": "Drop-in fasteners",
                        "category": "hardware",
                        "size_class": "small",
                        "size_source": "learned",
                        "size_confidence": 0.9,
                        "dimensions": {"width_mm": 10},
                        "active": True,
                        "archived_at": None,
                        "home": {"local_code": "B001"},
                        "last_seen": {"local_code": "B001"},
                        "item_aliases": [{"alias": "M4 tee nuts"}],
                        "photos": [{"local_id": 11}],
                    },
                    "locations": {"local_code": "B001"},
                    "inventory_review_photos": [
                        {"photo": {"local_id": 12}},
                    ],
                }
            ]

    client = JoinedInventoryClient()
    repository = SupabaseInventoryRepository(
        client,  # type: ignore[arg-type]
        workspace_id="workspace-1",
    )

    [result] = repository.list_inventory()

    assert result.item_id == 7
    assert result.name == "M4 T-nuts"
    assert result.aliases == ["M4 tee nuts"]
    assert result.bin_id == "B001"
    assert result.photos == ["/photos/11", "/photos/12"]
    assert result.needs_review is True
    assert result.home_location_id == "B001"
    assert client.calls == [
        (
            "inventory_entries",
            repository._location_content_columns(),
            {"workspace_id": "eq.workspace-1"},
            "item_id.asc,location_id.asc",
        )
    ]

    assert repository.list_inventory()[0].quantity == 4
    assert len(client.calls) == 1


def test_live_inventory_catalog_uses_one_bounded_rpc() -> None:
    requests = []
    response = {
        "total": 2,
        "metrics": {
            "items": 4,
            "placements": 5,
            "locations": 2,
            "need_review": 1,
        },
        "categories": ["electronics", "hardware"],
        "items": [
            {
                "item_id": 7,
                "name": "M2 bolts",
                "description": "",
                "category": "hardware",
                "aliases": ["metric bolts"],
                "bin_id": "B001",
                "quantity": 2,
                "unit": "each",
                "needs_review": False,
                "review_reason": "",
                "photo_ids": [11],
                "size_class": "small",
                "size_source": "agent",
                "size_confidence": 0.9,
                "dimensions": {"thread_diameter_mm": 2},
                "home_location_id": "B001",
                "last_seen_location_id": "B001",
                "active": True,
                "archived_at": None,
            },
            {
                "item_id": 7,
                "name": "M2 bolts",
                "description": "",
                "category": "hardware",
                "aliases": ["metric bolts"],
                "bin_id": "B002",
                "quantity": 3,
                "unit": "each",
                "needs_review": False,
                "review_reason": "",
                "photo_ids": [],
                "active": True,
                "archived_at": None,
            },
        ],
    }

    def transport(request, timeout):
        requests.append((request, timeout))
        return HttpResponse(200, json.dumps(response).encode("utf-8"))

    repository = SupabaseInventoryRepository(
        SupabaseRestClient(
            url="https://project.supabase.co",
            api_key="secret",
            transport=transport,
        ),
        workspace_id="workspace-1",
    )

    page = repository.inventory_catalog_page(
        query="M2 bolts",
        category="hardware",
        location_ids={"B002", "B001"},
        scope="all",
        sort="quantity",
        limit=1,
        offset=1,
    )

    assert page.total == 2
    assert len(page.items) == 2
    assert {row.bin_id for row in page.items} == {"B001", "B002"}
    assert page.items[0].photos == ["/photos/11"]
    assert page.metrics["placements"] == 5
    assert page.categories == ["electronics", "hardware"]
    assert len(requests) == 1
    request = requests[0][0]
    assert request.get_method() == "POST"
    assert request.full_url.endswith(
        "/rest/v1/rpc/magpie_inventory_catalog_page_v2"
    )
    arguments = json.loads(request.data.decode("utf-8"))
    assert arguments == {
        "target_workspace_id": "workspace-1",
        "include_archived": False,
        "search_query": "M2 bolts",
        "category_filter": "hardware",
        "scope_filter": "all",
        "sort_mode": "quantity",
        "page_limit": 1,
        "page_offset": 1,
        "location_filters": ["B001", "B002"],
    }


def test_live_inventory_catalog_falls_back_while_rpc_schema_cache_is_stale() -> None:
    responses = [
        HttpResponse(404, b'{"message":"function was not found"}'),
        HttpResponse(
            200,
            json.dumps(
                [
                    {
                        "id": "entry-1",
                        "item_id": "item-1",
                        "location_id": "location-1",
                        "quantity": 1,
                        "unit": "each",
                        "needs_review": False,
                        "review_reason": "",
                        "items": {
                            "local_id": 9,
                            "name": "USB cable",
                            "description": "",
                            "category": "electronics",
                            "size_class": "",
                            "size_source": "",
                            "size_confidence": 0,
                            "dimensions": None,
                            "active": True,
                            "archived_at": None,
                            "home": None,
                            "last_seen": None,
                            "item_aliases": [],
                            "photos": [],
                        },
                        "locations": {"local_code": "B001"},
                        "inventory_review_photos": [],
                    }
                ]
            ).encode("utf-8"),
        ),
    ]
    requests = []

    def transport(request, timeout):
        requests.append((request, timeout))
        return responses.pop(0)

    repository = SupabaseInventoryRepository(
        SupabaseRestClient(
            url="https://project.supabase.co",
            api_key="secret",
            transport=transport,
        ),
        workspace_id="workspace-1",
    )

    page = repository.inventory_catalog_page(query="USB", limit=10)

    assert [row.name for row in page.items] == ["USB cable"]
    assert page.total == 1
    assert len(requests) == 2
    assert requests[0][0].full_url.endswith(
        "/rest/v1/rpc/magpie_inventory_catalog_page_v2"
    )
    assert "/rest/v1/inventory_entries?" in requests[1][0].full_url

    second = repository.inventory_catalog_page(query="USB", limit=10)

    assert [row.name for row in second.items] == ["USB cable"]
    assert len(requests) == 2
    scoped = repository.inventory_catalog_page(location_ids={"B001"}, limit=10)
    excluded = repository.inventory_catalog_page(location_ids={"B999"}, limit=10)
    assert [row.name for row in scoped.items] == ["USB cable"]
    assert excluded.total == 0
    assert len(requests) == 2


def test_item_archive_restore_preserves_supabase_inventory() -> None:
    _, repository = _repository()
    item_id = repository.upsert_item(name="Retired jig")
    repository.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")

    assert repository.archive_item(item_id) is True
    assert repository.list_inventory() == []
    [archived] = repository.list_inventory(include_archived=True)
    assert archived.active is False
    assert archived.archived_at
    assert repository.occupied_bin_ids() == set()

    assert repository.restore_item(item_id) is True
    [restored] = repository.list_inventory()
    assert restored.active is True
    assert restored.archived_at is None
    assert restored.bin_id == "B001"


def test_item_location_metadata_restore_preserves_supabase_backup_state() -> None:
    client, repository = _repository()
    item_id = repository.upsert_item(name="Archived shoulder screw")
    repository.put_inventory(
        item_id=item_id, bin_id="B001", quantity=1, unit="each"
    )
    repository.put_inventory(
        item_id=item_id, bin_id="B002", quantity=2, unit="each"
    )

    repository.restore_item_location_metadata(
        item_id=item_id,
        home_location_id="B001",
        last_seen_location_id="B002",
        active=False,
        archived_at="2026-07-28T14:30:00+00:00",
    )

    item = next(row for row in client.tables["items"] if row["local_id"] == item_id)
    b001 = next(
        row for row in client.tables["locations"] if row["local_code"] == "B001"
    )
    b002 = next(
        row for row in client.tables["locations"] if row["local_code"] == "B002"
    )
    assert item["home_location_id"] == b001["id"]
    assert item["last_seen_location_id"] == b002["id"]
    assert item["active"] is False
    assert item["archived_at"] == "2026-07-28T14:30:00+00:00"
    restored = repository.list_inventory(include_archived=True)
    assert {row.home_location_id for row in restored} == {"B001"}
    assert {row.last_seen_location_id for row in restored} == {"B002"}
    assert all(row.active is False for row in restored)


def test_item_merge_preserves_supabase_identity_and_placements() -> None:
    client, repository = _repository()
    source_id = repository.upsert_item(name="M4 tee nuts", aliases=["tee nuts"])
    target_id = repository.upsert_item(name="M4 T-nuts", aliases=["M4 hardware"])
    repository.put_inventory(
        item_id=source_id, bin_id="B001", quantity=2, unit="each"
    )
    repository.put_inventory(
        item_id=target_id, bin_id="B001", quantity=3, unit="each"
    )
    repository.put_inventory(
        item_id=source_id, bin_id="B002", quantity=4, unit="each"
    )

    preview = repository.merge_item(
        source_item_id=source_id, target_item_id=target_id
    )

    assert preview["can_merge"] is True
    rows = repository.item_inventory(target_id)
    assert [(row.bin_id, row.quantity) for row in rows] == [
        ("B001", 5),
        ("B002", 4),
    ]
    assert {"M4 tee nuts", "tee nuts", "M4 hardware"} <= set(rows[0].aliases)
    assert repository.resolve_item_id(source_id) == target_id
    assert repository.restore_item(source_id) is False
    assert repository.exact_match("M4 tee nuts")[0].item_id == target_id
    assert repository.upsert_item(name="M4 tee nuts") == target_id
    source = next(row for row in client.tables["items"] if row["local_id"] == source_id)
    assert source["merged_into_item_id"]


def test_live_location_metrics_use_bounded_aggregate_rpcs() -> None:
    requests = []

    def transport(request, timeout):
        requests.append((request, timeout))
        return HttpResponse(
            200,
            b'[{"location_code":"B001","item_count":2,"review_count":1,'
            b'"content_revision":7},{"location_code":"B002","item_count":1,'
            b'"review_count":0,"content_revision":3}]',
        )

    client = SupabaseRestClient(
        url="https://project.supabase.co",
        api_key="secret",
        transport=transport,
    )
    repository = SupabaseInventoryRepository(client, workspace_id="workspace-1")

    assert repository.location_inventory_metrics() == {
        "B001": {"item_count": 2, "review_count": 1, "content_revision": 7},
        "B002": {"item_count": 1, "review_count": 0, "content_revision": 3},
    }
    assert repository.location_item_counts() == {"B001": 2, "B002": 1}
    assert repository.location_item_counts() == {"B001": 2, "B002": 1}
    assert repository.location_review_counts() == {"B001": 1}
    assert len(requests) == 3
    for request, _ in requests:
        assert request.full_url.endswith(
            "/rest/v1/rpc/magpie_location_inventory_metrics"
        )
        assert json.loads(request.data.decode("utf-8")) == {
            "target_workspace_id": "workspace-1"
        }


def test_live_location_metrics_reject_invalid_rpc_payload() -> None:
    requests = []

    def transport(request, timeout):
        requests.append((request, timeout))
        return HttpResponse(200, b'{"location_code":"B001"}')

    client = SupabaseRestClient(
        url="https://project.supabase.co",
        api_key="secret",
        transport=transport,
    )
    repository = SupabaseInventoryRepository(client, workspace_id="workspace-1")

    with pytest.raises(RuntimeError, match="invalid response"):
        repository.location_inventory_metrics()
    assert len(requests) == 1


def test_live_location_contents_page_hydrates_only_returned_entries() -> None:
    location_id = str(uuid4())
    item_id = str(uuid4())
    entry_id = str(uuid4())
    responses = [
        HttpResponse(
            206,
            json.dumps(
                [
                    {
                        "id": entry_id,
                        "workspace_id": "workspace-1",
                        "item_id": item_id,
                        "location_id": location_id,
                        "quantity": 4,
                        "unit": "each",
                        "needs_review": False,
                        "review_reason": "",
                        "items": {
                            "local_id": 77,
                            "name": "M4 washers",
                            "description": "",
                            "category": "fasteners",
                            "size_class": "small",
                            "size_source": "agent",
                            "size_confidence": 0.9,
                            "dimensions": None,
                            "home": {"local_code": "B001"},
                            "last_seen": {"local_code": "B001"},
                            "item_aliases": [{"alias": "metric washers"}],
                            "photos": [{"local_id": 9}],
                        },
                        "locations": {"local_code": "B001"},
                        "inventory_review_photos": [
                            {"photo": {"local_id": 10}}
                        ],
                    }
                ]
            ).encode("utf-8"),
            {"Content-Range": "30-30/138"},
        )
    ]
    requests = []

    def transport(request, timeout):
        requests.append((request, timeout))
        return responses.pop(0)

    repository = SupabaseInventoryRepository(
        SupabaseRestClient(
            url="https://project.supabase.co",
            api_key="secret",
            transport=transport,
        ),
        workspace_id="workspace-1",
    )

    page, total = repository.items_in_locations_page(
        ["B001"], limit=30, offset=30
    )

    assert total == 138
    assert len(page) == 1
    assert page[0].item_id == 77
    assert page[0].aliases == ["metric washers"]
    assert page[0].photos == ["/photos/9", "/photos/10"]
    assert page[0].home_location_id == "B001"
    assert page[0].last_seen_location_id == "B001"
    assert len(requests) == 1
    entry_request = requests[0][0]
    entry_query = parse_qs(urlparse(entry_request.full_url).query)
    assert entry_query["locations.local_code"] == ['in.("B001")']
    assert "items!inner" in entry_query["select"][0]
    assert "inventory_review_photos" in entry_query["select"][0]
    assert entry_request.headers["Range"] == "30-59"


def test_live_filtered_location_contents_pages_workspace_matches() -> None:
    direct_item_id = str(uuid4())
    alias_item_id = str(uuid4())
    entry_id = str(uuid4())
    responses = [
        HttpResponse(200, json.dumps([{"id": direct_item_id}]).encode("utf-8")),
        HttpResponse(
            200,
            json.dumps(
                [{"item_id": alias_item_id, "items": {"workspace_id": "workspace-1"}}]
            ).encode("utf-8"),
        ),
        HttpResponse(
            206,
            json.dumps(
                [
                    {
                        "id": entry_id,
                        "item_id": alias_item_id,
                        "location_id": str(uuid4()),
                        "quantity": 1,
                        "unit": "each",
                        "needs_review": False,
                        "review_reason": "",
                        "items": {
                            "local_id": 88,
                            "name": "Metric fastener kit",
                            "description": "",
                            "category": "fasteners",
                            "size_class": "small",
                            "size_source": "agent",
                            "size_confidence": 0.9,
                            "dimensions": None,
                            "home": {"local_code": "B001"},
                            "last_seen": {"local_code": "B001"},
                            "item_aliases": [{"alias": "M4 washers"}],
                            "photos": [],
                        },
                        "locations": {"local_code": "B001"},
                        "inventory_review_photos": [],
                    }
                ]
            ).encode("utf-8"),
            {"Content-Range": "0-0/1"},
        ),
    ]
    requests = []

    def transport(request, timeout):
        requests.append((request, timeout))
        return responses.pop(0)

    repository = SupabaseInventoryRepository(
        SupabaseRestClient(
            url="https://project.supabase.co",
            api_key="secret",
            transport=transport,
        ),
        workspace_id="workspace-1",
    )

    page, total = repository.items_in_locations_page(
        ["B001"], query="M4 washer", limit=30
    )

    assert total == 1
    assert [item.item_id for item in page] == [88]
    assert len(requests) == 3
    item_query = parse_qs(urlparse(requests[0][0].full_url).query)
    alias_query = parse_qs(urlparse(requests[1][0].full_url).query)
    entry_query = parse_qs(urlparse(requests[2][0].full_url).query)
    assert item_query["workspace_id"] == ["eq.workspace-1"]
    assert item_query["or"] == [
        '(name.ilike."*m4 washer*",category.ilike."*m4 washer*",'
        'description.ilike."*m4 washer*")'
    ]
    assert alias_query["select"] == ["item_id,items!inner(workspace_id)"]
    assert alias_query["alias"] == ['ilike."*m4 washer*"']
    assert alias_query["items.workspace_id"] == ["eq.workspace-1"]
    assert entry_query["locations.local_code"] == ['in.("B001")']
    candidate_ids = (
        entry_query["item_id"][0]
        .removeprefix("in.(")
        .removesuffix(")")
        .split(",")
    )
    assert set(candidate_ids) == {direct_item_id, alias_item_id}
    assert requests[2][0].headers["Range"] == "0-29"


def test_live_global_search_intersects_terms_and_pages_inventory() -> None:
    shared_item_id = str(uuid4())
    other_item_id = str(uuid4())
    responses = [
        HttpResponse(
            200,
            json.dumps([{"id": shared_item_id}, {"id": other_item_id}]).encode(
                "utf-8"
            ),
        ),
        HttpResponse(200, b"[]"),
        HttpResponse(200, json.dumps([{"id": shared_item_id}]).encode("utf-8")),
        HttpResponse(200, b"[]"),
        HttpResponse(
            206,
            json.dumps(
                [
                    {
                        "id": str(uuid4()),
                        "item_id": shared_item_id,
                        "location_id": str(uuid4()),
                        "quantity": 12,
                        "unit": "each",
                        "needs_review": False,
                        "review_reason": "",
                        "items": {
                            "local_id": 91,
                            "name": "M4 T-nuts",
                            "description": "",
                            "category": "hardware",
                            "size_class": "small",
                            "size_source": "agent",
                            "size_confidence": 0.9,
                            "dimensions": None,
                            "home": {"local_code": "B010"},
                            "last_seen": {"local_code": "B010"},
                            "item_aliases": [{"alias": "tee nuts"}],
                            "photos": [{"local_id": 11}],
                        },
                        "locations": {"local_code": "B010"},
                        "inventory_review_photos": [],
                    }
                ]
            ).encode("utf-8"),
            {"Content-Range": "0-0/1"},
        ),
    ]
    requests = []

    def transport(request, timeout):
        requests.append((request, timeout))
        return responses.pop(0)

    repository = SupabaseInventoryRepository(
        SupabaseRestClient(
            url="https://project.supabase.co",
            api_key="secret",
            transport=transport,
        ),
        workspace_id="workspace-1",
    )

    results = repository.search("M4 hardware", limit=20)

    assert [result.item_id for result in results] == [91]
    assert results[0].photos == ["/photos/11"]
    assert len(requests) == 5
    candidate_queries = [
        parse_qs(urlparse(request[0].full_url).query) for request in requests[:4]
    ]
    assert all(
        query.get("workspace_id") == ["eq.workspace-1"]
        for query in (candidate_queries[0], candidate_queries[2])
    )
    entry_query = parse_qs(urlparse(requests[4][0].full_url).query)
    assert entry_query["workspace_id"] == ["eq.workspace-1"]
    assert entry_query["item_id"] == [f"in.({shared_item_id})"]
    assert requests[4][0].headers["Range"] == "0-19"


def test_location_photos_bulk_read_uses_one_joined_postgrest_query() -> None:
    requests = []

    def transport(request, timeout):
        requests.append((request, timeout))
        return HttpResponse(
            200,
            json.dumps(
                [
                    {
                        "id": "photo-uuid",
                        "workspace_id": "workspace-1",
                        "local_id": 7,
                        "item_id": None,
                        "location_id": "location-uuid",
                        "filename": "drawer.jpg",
                        "content_type": "image/jpeg",
                        "storage_path": "workspace-1/drawer.jpg",
                        "items": None,
                        "locations": {"local_code": "drawer-one"},
                    }
                ]
            ).encode("utf-8"),
        )

    repository = SupabaseInventoryRepository(
        SupabaseRestClient(
            url="https://project.supabase.co",
            api_key="secret",
            transport=transport,
        ),
        workspace_id="workspace-1",
    )

    grouped = repository.location_photos_by_location(["drawer-one", "missing"])

    assert grouped["drawer-one"][0].id == 7
    assert grouped["drawer-one"][0].location_id == "drawer-one"
    assert len(requests) == 1
    query = parse_qs(urlparse(requests[0][0].full_url).query)
    assert query["select"] == ["*,items(local_id),locations!inner(local_code)"]
    assert query["workspace_id"] == ["eq.workspace-1"]


def test_alias_learning_and_clear_bin() -> None:
    _, repository = _repository()
    first = repository.upsert_item(name="Hex keys")
    second = repository.upsert_item(name="Small wrench")
    repository.put_inventory(item_id=first, bin_id="B001", quantity=1, unit="set")
    repository.put_inventory(item_id=second, bin_id="B001", quantity=1, unit="each")

    repository.learn_alias(item_id=first, alias="Allen keys")
    assert repository.search("allen")[0].item_id == first
    assert repository.remove_alias(item_id=first, alias="ALLEN KEYS")
    assert repository.search("allen") == []
    assert repository.clear_bin_inventory("B001") == 2
    assert repository.items_in_locations(["B001", "B002"]) == []


def test_alias_cannot_identify_two_supabase_hardware_variants() -> None:
    _, repository = _repository()
    short = repository.upsert_item(name="M4 x 12 mm screws")
    long = repository.upsert_item(name="M4 x 20 mm screws")
    repository.put_inventory(item_id=short, bin_id="B001", quantity=10, unit="pieces")
    repository.put_inventory(item_id=long, bin_id="B002", quantity=10, unit="pieces")
    repository.learn_alias(item_id=long, alias="my M4 screws")

    with pytest.raises(ValueError, match="already identifies M4 x 20 mm screws"):
        repository.learn_alias(item_id=short, alias="my M4 screws")

    assert repository.exact_match("my M4 screws")[0].item_id == long


def test_size_learning_home_and_rule_corrections() -> None:
    _, repository = _repository()
    item_id = repository.upsert_item(name="Jewelry box", aliases=["ring case"])
    repository.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")
    repository.update_item_size(
        item_id=item_id,
        size_class="med",
        source="user",
        confidence=0.9,
        dimensions={"width_mm": 120},
    )
    repository.update_item_home(item_id=item_id, location_id="B002")

    result = repository.item_inventory(item_id)[0]
    assert result.size_class == "medium"
    assert result.dimensions == {"width_mm": 120}
    assert result.home_location_id == "B002"
    assert repository.item_size_class("ring case") == "medium"

    repository.restore_item_size_metadata(
        item_id=item_id,
        size_class="small",
        source="user correction",
        confidence=0.7,
        dimensions={"width_mm": 90},
    )
    restored_size = repository.item_inventory(item_id)[0]
    assert restored_size.size_class == "small"
    assert restored_size.size_source == "user correction"
    assert restored_size.size_confidence == 0.7
    assert restored_size.dimensions == {"width_mm": 90}

    placement_id = repository.learn_rule(
        rule_text="Jewelry belongs in the main cabinet",
        trigger_terms=["jewelry", "rings"],
        target_location_id="B001",
    )
    size_rule_id = repository.learn_size_rule(
        rule_text="Jewelry boxes are medium",
        trigger_terms=["jewelry", "boxes"],
        size_class="medium",
    )
    assert repository.matching_rules("put away jewelry")[0].id == placement_id
    assert repository.matching_size_rules("jewelry box")[0].id == size_rule_id

    assert repository.update_rule(
        rule_id=placement_id,
        rule_text="Jewelry belongs in the second drawer",
        trigger_terms=["jewelry", "rings"],
        target_location_id="B002",
    )
    assert repository.rule(placement_id).target_location_id == "B002"
    assert repository.deactivate_rule(rule_id=size_rule_id, rule_type="size")
    assert repository.size_rule(size_rule_id) is None
    assert repository.set_rule_active(
        rule_id=size_rule_id, rule_type="size", active=True
    )
    assert repository.size_rule(size_rule_id).size_class == "medium"
    assert repository.set_rule_active(
        rule_id=size_rule_id, rule_type="size", active=False
    )

    repository.clear_item_size(item_id=item_id)
    assert repository.item_inventory(item_id)[0].size_class == ""


def test_supabase_confirmed_home_observations_are_reversible_and_lockable() -> None:
    _, repository = _repository()
    item_id = repository.upsert_item(name="USB adapters")
    repository.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")

    first = repository.observe_confirmed_home(item_id=item_id, location_id="B002")
    second = repository.observe_confirmed_home(item_id=item_id, location_id="B002")

    assert first["promoted"] is False
    assert second["promoted"] is True
    assert repository.suggest_home("USB adapters") == "B002"
    assert repository.undo_confirmed_home_observation(int(second["home_observation"]))
    repository.update_item_home(item_id=item_id, location_id="B001")
    locked = repository.observe_confirmed_home(item_id=item_id, location_id="B002")
    assert locked["explicit_home"] is True
    assert locked["promoted"] is False
    assert repository.suggest_home("USB adapters") == "B001"


def test_rule_conflicts_match_sqlite_behavior() -> None:
    _, repository = _repository()
    repository.learn_rule(
        rule_text="Nuts go left",
        trigger_terms=["nuts", "hardware"],
        target_location_id="B001",
    )
    repository.learn_rule(
        rule_text="Nuts go right",
        trigger_terms=["nuts", "fasteners"],
        target_location_id="B002",
    )

    conflict = repository.rule_conflicts("nuts")[0]
    assert conflict["rule_type"] == "placement"
    assert conflict["shared_terms"] == ["nuts"]


def test_learning_examples_review_and_retrieval() -> None:
    _, repository = _repository()
    repository.record_example(
        command="Put jewelry in B001",
        action='{"plan":[{"action":"put_item"}]}',
        kind="confirmed",
        context={"source": "voice"},
    )
    repository.record_example(
        command="Actually keep jewelry in B002",
        action='{"plan":[{"location_id":"B002"}]}',
        kind="correction",
    )

    examples = repository.find_examples("jewelry")
    assert [example["kind"] for example in examples] == ["correction", "confirmed"]
    correction_id = examples[0]["id"]
    assert repository.approve_example(correction_id)
    assert repository.example(correction_id)["review_status"] == "approved"
    assert repository.example_review_counts() == {
        "pending": 1,
        "approved": 1,
        "promoted": 0,
        "rejected": 0,
        "active": 2,
        "total": 2,
    }
    assert repository.example_kind_counts() == {"confirmed": 1, "correction": 1}
    assert repository.mark_example_promoted(correction_id)


def test_supabase_review_reads_are_filtered_and_bounded() -> None:
    client, repository = _repository()
    item_id = repository.upsert_item(name="Uncertain washer")
    repository.put_inventory(
        item_id=item_id,
        bin_id="B001",
        quantity=1,
        unit="each",
        needs_review=True,
        review_reason="Confirm washer size",
    )
    repository.record_example(
        command="Put away an uncertain washer",
        action='[{"tool":"put_item","arguments":{"item":"washer"}}]',
        kind="correction",
    )

    client.select_history.clear()
    [item] = repository.review_queue(limit=7)
    assert item.name == "Uncertain washer"
    review_query = client.select_history[0]
    assert review_query == {
        "table": "inventory_entries",
        "filters": {
            "workspace_id": "eq.workspace-1",
            "needs_review": "eq.true",
            "items.active": "eq.true",
        },
        "order": "updated_at.desc",
        "limit": 7,
    }

    client.select_history.clear()
    examples = repository.examples_for_eval(
        limit=7,
        kind="correction",
        review_status="pending",
    )
    assert len(examples) == 1
    assert client.select_history == [
        {
            "table": "agent_examples",
            "filters": {
                "workspace_id": "eq.workspace-1",
                "kind": "eq.correction",
                "review_status": "eq.pending",
            },
            "order": "local_id.desc",
            "limit": 7,
        }
    ]


def test_new_supabase_agent_plan_supersedes_older_pending_plans() -> None:
    client, repository = _repository()
    old_id = repository.record_assistant_interaction(
        route="/assistant/chat",
        command="Put away old T-nuts",
        response={"plan": [{"action": "put_item"}]},
        status="pending",
    )

    client.select_history.clear()
    assert repository.supersede_pending_agent_plans() == 1
    assert client.select_history == []
    new_id = repository.record_assistant_interaction(
        route="/assistant/chat",
        command="Put away current T-nuts",
        response={"plan": [{"action": "put_item"}]},
        status="pending",
    )

    assert repository.assistant_interaction(old_id)["status"] == "superseded"
    assert repository.latest_pending_agent_plan()["id"] == new_id
    latest_query = client.select_history[-1]
    assert latest_query == {
        "table": "assistant_interactions",
        "filters": {
            "workspace_id": "eq.workspace-1",
            "status": "eq.pending",
            "route": (
                'in.("/assistant/agent","/assistant/chat","/assistant/revise",'
                '"/mcp/propose_plan")'
            ),
        },
        "order": "local_id.desc",
        "limit": 1,
    }


def test_idle_kiosk_reuses_bounded_interaction_queries_until_a_write(
    tmp_path,
) -> None:
    client, inventory = _repository()
    locations = SupabaseLocationRepository(client, workspace_id="workspace-1")
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps(
            {
                "name": "supabase-idle-poll",
                "columns": 1,
                "rows_bottom_to_top": [],
            }
        ),
        encoding="utf-8",
    )
    api = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout),
                database_path=str(tmp_path / "unused.db"),
            ),
            repositories=StorageRepositories(
                inventory=inventory,
                locations=locations,
                backend="supabase",
                workspace_id="workspace-1",
            ),
        )
    )
    client.select_history.clear()

    assert api.get("/kiosk/state?wait_seconds=0").status_code == 200
    first_queries = [
        query
        for query in client.select_history
        if query["table"] == "assistant_interactions"
    ]
    assert [query["limit"] for query in first_queries] == [10, 1]
    assert not any(query["limit"] == 100 for query in first_queries)

    assert api.get("/kiosk/state?wait_seconds=0").status_code == 200
    assert [
        query
        for query in client.select_history
        if query["table"] == "assistant_interactions"
    ] == first_queries

    created = api.post(
        "/locations",
        json={"type": "room", "label": "Garage", "accepts_items": False},
    )
    assert created.status_code == 200
    assert api.get("/kiosk/state?wait_seconds=0").status_code == 200
    refreshed_queries = [
        query
        for query in client.select_history
        if query["table"] == "assistant_interactions"
    ]
    assert [query["limit"] for query in refreshed_queries] == [10, 1, 10, 1]


def test_storage_usage_snapshot_does_not_query_supabase(tmp_path) -> None:
    client, inventory = _repository()
    client.usage_snapshot = lambda: {
        "schema": "magpie.supabase_client_usage.v1",
        "started_at": "2026-08-01T12:00:00Z",
        "logical_requests": 14,
        "response_bytes": 2048,
        "errors": 1,
        "elapsed_seconds": 120.0,
        "requests_per_minute": 7.0,
        "response_bytes_per_minute": 1024.0,
        "operations": [
            {
                "method": "GET",
                "operation": "rest/assistant_interactions",
                "requests": 8,
                "response_bytes": 1536,
                "errors": 0,
            }
        ],
    }
    locations = SupabaseLocationRepository(client, workspace_id="workspace-1")
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "usage", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    api = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout),
                database_path=str(tmp_path / "unused.db"),
            ),
            repositories=StorageRepositories(
                inventory=inventory,
                locations=locations,
                backend="supabase",
                workspace_id="workspace-1",
            ),
        )
    )
    before = client.select_calls

    response = api.get("/usage/storage")

    assert response.status_code == 200
    assert client.select_calls == before
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "ok": True,
        "schema": "magpie.storage_usage.v1",
        "backend": "supabase",
        "available": True,
        "observed": client.usage_snapshot(),
        "measurement": (
            "Logical Magpie requests and final response-body bytes observed "
            "since this service process started."
        ),
        "billing_authority": (
            "Supabase Usage is authoritative for billed egress, retries, and "
            "protocol overhead."
        ),
    }


def test_kiosk_confirmation_status_uses_an_exact_bounded_supabase_read(
    tmp_path,
) -> None:
    client, inventory = _repository()
    locations = SupabaseLocationRepository(client, workspace_id="workspace-1")
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps(
            {
                "name": "supabase-confirmation-state",
                "columns": 1,
                "rows_bottom_to_top": [],
            }
        ),
        encoding="utf-8",
    )
    api = TestClient(
        create_app(
            LedConfig(
                pixel_count=0,
                backend="none",
                layout_path=str(layout),
                database_path=str(tmp_path / "unused.db"),
            ),
            repositories=StorageRepositories(
                inventory=inventory,
                locations=locations,
                backend="supabase",
                workspace_id="workspace-1",
            ),
        )
    )
    interaction_id = inventory.record_assistant_interaction(
        route="/assistant/chat",
        command="move and label the hardware",
        response={
            "plan": [
                {"tool": "move_item", "arguments": {"item": "hardware"}},
                {"tool": "print_label", "arguments": {"location_id": "B001"}},
            ]
        },
        status="pending",
    )
    inventory.set_assistant_interaction_status(interaction_id, "superseded")
    client.select_history.clear()

    state = api.get(
        "/kiosk/state",
        params={
            "after_id": interaction_id,
            "confirmation_interaction_id": interaction_id,
            "wait_seconds": 0,
        },
    ).json()

    assert state["pending_agent_plan"] is None
    assert state["confirmation_state"]["status"] == "superseded"
    interaction_queries = [
        query
        for query in client.select_history
        if query["table"] == "assistant_interactions"
    ]
    assert all(query["limit"] in {1, 10} for query in interaction_queries)
    assert any(
        query["filters"] == {
            "workspace_id": "eq.workspace-1",
            "local_id": f"eq.{interaction_id}",
        }
        and query["limit"] == 1
        for query in interaction_queries
    )


def test_supabase_client_request_replay_uses_exact_bounded_query() -> None:
    client, repository = _repository()
    interaction_id = repository.record_assistant_interaction(
        route="/locations/B001/quick-put",
        command="blue pry tool",
        response={
            "client_request_id": "scan-request-123",
            "client_response": {"ok": True},
        },
    )
    client.select_history.clear()

    found = repository.assistant_interaction_by_client_request_id(
        "scan-request-123",
        route="/locations/B001/quick-put",
    )

    assert found is not None
    assert found["id"] == interaction_id
    assert client.select_history == [
        {
            "table": "assistant_interactions",
            "filters": {
                "workspace_id": "eq.workspace-1",
                "response->>client_request_id": "eq.scan-request-123",
                "route": "eq./locations/B001/quick-put",
            },
            "order": "local_id.desc",
            "limit": 1,
        }
    ]


def test_supabase_active_guidance_state_round_trips() -> None:
    _, repository = _repository()
    state = {
        "entries": [
            {
                "id": 1,
                "kind": "find",
                "query": "USB drive",
                "color": [255, 0, 0],
                "matches": [],
            }
        ],
        "next_id": 2,
    }

    assert repository.load_active_guidance_state() == {
        "entries": [],
        "next_id": 1,
    }
    repository.save_active_guidance_state(state)

    assert repository.load_active_guidance_state() == state


def test_assistant_plans_voice_attempts_and_undo_lookup() -> None:
    client, repository = _repository()
    pending_id = repository.record_assistant_interaction(
        route="/assistant/chat",
        command="Put away T-nuts",
        photo_ids=[4],
        response={"plan": [{"action": "put_item"}]},
        status="pending",
    )
    assert repository.latest_pending_agent_plan()["id"] == pending_id
    assert repository.update_assistant_interaction_response(
        pending_id, {"latency_ms": 120}
    )
    assert repository.assistant_interaction(pending_id)["response"]["latency_ms"] == 120
    assert repository.claim_assistant_interaction_confirmation(pending_id) is True
    assert repository.claim_assistant_interaction_confirmation(pending_id) is False
    assert repository.assistant_interaction(pending_id)["status"] == "confirming"

    repository.record_assistant_interaction(
        route="/assistant/chat",
        command="Find T-nuts",
        response={"action": "found", "plan": []},
    )
    assert repository.latest_pending_agent_plan() is None

    confirmed_id = repository.record_assistant_interaction(
        route="/assistant/confirm",
        command="Confirm",
        response={"applied": [{"action": "put_item"}]},
        status="confirmed",
    )
    client.select_history.clear()
    assert repository.latest_undoable_confirmed_plan()["id"] == confirmed_id
    undo_query = client.select_history[-1]
    assert undo_query["table"] == "assistant_interactions"
    assert undo_query["filters"]["workspace_id"] == "eq.workspace-1"
    assert undo_query["filters"]["status"] == "neq.undone"
    assert undo_query["filters"]["route"].startswith("in.(")
    assert undo_query["order"] == "local_id.desc"
    assert undo_query["limit"] == 25
    assert repository.set_assistant_interaction_status(confirmed_id, "undone")
    assert repository.latest_undoable_confirmed_plan() is None

    archive_id = repository.record_assistant_interaction(
        route="/locations/archive",
        command="manual archive location attic",
        response={
            "applied": [
                {
                    "archived_location_id": "attic",
                    "archived_ids": ["attic", "winter-tote"],
                }
            ]
        },
        status="manual",
    )
    assert repository.latest_undoable_confirmed_plan()["id"] == archive_id
    assert repository.set_assistant_interaction_status(archive_id, "undone")

    update_id = repository.record_assistant_interaction(
        route="/locations/update",
        command="manual update location attic",
        response={
            "applied": [
                {
                    "updated_location": "attic",
                    "previous_label": "Attic",
                }
            ]
        },
        status="manual",
    )
    assert repository.latest_undoable_confirmed_plan()["id"] == update_id
    assert repository.set_assistant_interaction_status(update_id, "undone")

    voice_id = repository.record_voice_attempt(
        rms=2200,
        activity_rms=2450,
        transcript="Magpie find T-nuts",
        wake_word="magpie",
        wake_heard=True,
        command="find T-nuts",
        route="/assistant/chat",
        query="T-nuts",
        stt_model="whisper",
        audio_threshold=1900,
        device_id="main-cabinet",
        runtime_version="v0.1.0 abc1234",
        backend_response={"mode": "find"},
    )
    attempts = repository.recent_voice_command_attempts()
    assert attempts[0]["id"] == voice_id
    assert attempts[0]["device_id"] == "main-cabinet"
    assert attempts[0]["runtime_version"] == "v0.1.0 abc1234"
    assert attempts[0]["backend_response"] == {"mode": "find"}


def test_assistant_interaction_queries_are_filtered_and_bounded_in_supabase() -> None:
    client, repository = _repository()
    for interaction_id in range(1, 1201):
        client.tables["assistant_interactions"].append(
            {
                "id": str(uuid4()),
                "workspace_id": "workspace-1",
                "local_id": interaction_id,
                "route": "/assistant/chat",
                "command": f"command {interaction_id}",
                "photo_local_ids": [],
                "status": "",
                "response": {"payload": "x" * 1000},
            }
        )

    assert [
        row["id"] for row in repository.assistant_interactions_after(limit=3)
    ] == [1198, 1199, 1200]
    assert [
        row["id"]
        for row in repository.assistant_interactions_after(after_id=1197, limit=2)
    ] == [1198, 1199]
    before_bulk = client.select_calls
    assert [
        row["id"]
        for row in repository.assistant_interactions_by_ids([1199, 2, 1199, 0])
    ] == [2, 1199]
    assert client.select_calls == before_bulk + 1
    assert client.select_history[-1] == {
        "table": "assistant_interactions",
        "filters": {
            "workspace_id": "eq.workspace-1",
            "local_id": 'in.("2","1199")',
        },
        "order": "local_id.asc",
        "limit": 2,
    }
    before_empty = client.select_calls
    assert repository.assistant_interactions_by_ids([]) == []
    assert client.select_calls == before_empty


def test_recent_voice_queries_are_bounded_to_the_newest_rows() -> None:
    client, repository = _repository()
    client.tables["voice_attempts"] = [
        {
            "id": str(uuid4()),
            "workspace_id": "workspace-1",
            "local_id": attempt_id,
            "rms": 100.0,
            "activity_rms": 100.0,
            "transcript": "",
            "wake_word": "magpie",
            "wake_heard": False,
            "awaiting_command": False,
            "command": f"find item {attempt_id}" if attempt_id % 200 == 0 else "",
            "route": "/assistant/chat" if attempt_id % 200 == 0 else "",
            "query": f"item {attempt_id}" if attempt_id % 200 == 0 else "",
            "stt_model": "test",
            "audio_threshold": 800.0,
            "backend_response": {},
            "error": "quiet" if attempt_id % 200 else "",
            "created_at": "2026-07-15T00:00:00Z",
        }
        for attempt_id in range(1, 1201)
    ]

    assert [row["id"] for row in repository.recent_voice_attempts(limit=5)] == [
        1196,
        1197,
        1198,
        1199,
        1200,
    ]
    assert [
        row["id"] for row in repository.recent_voice_command_attempts(limit=2)
    ] == [1000, 1200]


def test_recent_voice_command_attempts_exclude_intermediate_listening_state() -> None:
    client, repository = _repository()
    repository.record_voice_attempt(
        rms=900,
        activity_rms=1200,
        wake_word="magpie",
        wake_heard=True,
        awaiting_command=True,
        stt_model="local-pocketsphinx",
        audio_threshold=800,
        error="awaiting_command_silence",
    )
    repository.record_voice_attempt(
        rms=900,
        activity_rms=1200,
        wake_word="magpie",
        wake_heard=True,
        awaiting_command=False,
        stt_model="local-pocketsphinx",
        audio_threshold=800,
        error="awaiting_command_timeout",
    )
    client.tables["voice_attempts"].append(
        {
            "id": str(uuid4()),
            "workspace_id": "workspace-1",
            "local_id": 2,
            "wake_heard": False,
            "awaiting_command": False,
            "command": "",
            "query": "",
            "error": "quiet",
        }
    )

    attempts = repository.recent_voice_command_attempts(limit=5)

    assert len(attempts) == 1
    assert attempts[0]["wake_heard"] is True
    assert attempts[0]["error"] == "awaiting_command_timeout"


def test_photo_upload_attachment_review_and_deletion() -> None:
    client, repository = _repository()
    item_id = repository.upsert_item(name="Blue pry tool")
    repository.put_inventory(item_id=item_id, bin_id="B001", quantity=1, unit="each")

    photo = repository.add_photo(
        filename="phone capture.jpeg",
        content_type="image/jpeg",
        data=b"jpeg-data",
    )
    assert photo.filename == "phone capture.jpeg"
    assert photo.url == f"/photos/{photo.id}"
    assert list(client.objects.values()) == [(b"jpeg-data", "image/jpeg")]
    assert repository.photo_content(photo.id) == b"jpeg-data"

    repository.attach_photo(item_id=item_id, photo_id=photo.id)
    attached = repository.photo(photo.id)
    assert attached.item_id == item_id
    assert repository.item_inventory(item_id)[0].photos == [photo.url]

    repository.attach_photo_to_location(location_id="B001", photo_id=photo.id)
    assert repository.location_photos("B001")[0].location_id == "B001"
    repository.link_review_photo(item_id=item_id, bin_id="B001", photo_id=photo.id)
    assert len(client.tables["inventory_review_photos"]) == 1

    repository.delete_photo(photo.id)
    assert repository.photo(photo.id) is None
    assert client.objects == {}
    with pytest.raises(ValueError, match=f"photo {photo.id} not found"):
        repository.attach_photo(item_id=item_id, photo_id=photo.id)


def test_atomic_photo_putaway_adds_quantity_and_rolls_back_missing_photo() -> None:
    client, repository = _repository()
    item_id = repository.upsert_item(name="M4 T-nuts")
    repository.put_inventory(item_id=item_id, bin_id="B001", quantity=4, unit="pieces")
    photo = repository.add_photo(
        filename="t-nuts.jpeg",
        content_type="image/jpeg",
        data=b"photo",
    )

    applied = repository.put_item_with_photos_atomic(
        item="M4 T-nuts",
        location_id="b001",
        quantity=2,
        unit="pieces",
        quantity_mode="add",
        photo_ids=[photo.id, photo.id],
        needs_review=True,
        review_reason="confirm photo",
    )

    assert applied == {
        "stored": "M4 T-nuts",
        "item_id": item_id,
        "location_id": "B001",
        "needs_review": True,
        "previous_quantity": 4.0,
        "previous_unit": "pieces",
        "quantity": 6.0,
    }
    result = repository.item_inventory(item_id)[0]
    assert result.quantity == 6
    assert result.photos == [f"/photos/{photo.id}"]
    assert result.review_reason == "confirm photo"
    assert len(client.tables["inventory_review_photos"]) == 1

    before = deepcopy(client.tables)
    with pytest.raises(ValueError, match="photos were not found"):
        repository.put_item_with_photos_atomic(
            item="Never created",
            location_id="B001",
            quantity=1,
            unit="each",
            quantity_mode="set",
            photo_ids=[99999],
        )
    assert client.tables == before


def test_atomic_inventory_import_sends_locations_and_rows_in_one_rpc() -> None:
    class ImportClient:
        def __init__(self):
            self.calls = []

        def rpc(self, function, arguments=None):
            self.calls.append((function, deepcopy(arguments or {})))
            return {
                "location_ids": ["garage", "garage-parts"],
                "rows": [
                    {
                        "item_id": 7,
                        "name": "M4 T-nuts",
                        "location_id": "garage-parts",
                        "quantity": 12,
                        "unit": "each",
                    }
                ],
            }

    client = ImportClient()
    repository = SupabaseInventoryRepository(
        client,
        workspace_id="workspace-1",
    )

    imported = repository.import_inventory_rows_atomic(
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
                "category": "hardware",
                "description": "slot fasteners",
                "aliases": ["tee nuts"],
            }
        ],
    )

    assert imported[0]["item_id"] == 7
    assert client.calls == [
        (
            "magpie_import_inventory",
            {
                "target_workspace_id": "workspace-1",
                "location_rows": [
                    {
                        "local_code": "garage",
                        "parent_code": None,
                        "type": "room",
                        "label": "Garage",
                        "capacity_class": "medium",
                        "accepts_items": False,
                    },
                    {
                        "local_code": "garage-parts",
                        "parent_code": "garage",
                        "type": "bin",
                        "label": "Parts",
                        "capacity_class": "medium",
                        "accepts_items": True,
                    },
                ],
                "inventory_rows": [
                    {
                        "name": "M4 T-nuts",
                        "location_code": "garage-parts",
                        "quantity": 12.0,
                        "unit": "each",
                        "category": "hardware",
                        "description": "slot fasteners",
                        "aliases": ["tee nuts"],
                    }
                ],
            },
        )
    ]


def test_guided_location_audit_uses_one_atomic_rpc() -> None:
    class AuditClient:
        def __init__(self):
            self.calls = []

        def rpc(self, function, arguments=None):
            self.calls.append((function, deepcopy(arguments or {})))
            return {
                "updates": [
                    {
                        "item_id": 7,
                        "location_id": "metric-bin",
                        "status": "missing",
                        "previous_needs_review": False,
                        "previous_review_reason": "",
                    }
                ]
            }

    client = AuditClient()
    repository = SupabaseInventoryRepository(client, workspace_id="workspace-1")
    placements = [
        {
            "item_id": 7,
            "location_id": "metric-bin",
            "expected_quantity": 12.0,
            "expected_unit": "each",
            "expected_needs_review": False,
            "status": "missing",
            "review_reason": "Reported missing during contents check of Fasteners.",
        }
    ]

    updates = repository.apply_location_audit(
        location_id="fasteners",
        location_ids=["fasteners", "metric-bin"],
        placements=placements,
        verification_note='{"schema":"magpie.location_verification.v2"}',
    )

    assert updates[0]["status"] == "missing"
    assert client.calls == [
        (
            "magpie_apply_location_audit",
            {
                "target_workspace_id": "workspace-1",
                "root_location_code": "fasteners",
                "audit_rows": placements,
                "verification_note": '{"schema":"magpie.location_verification.v2"}',
            },
        )
    ]


def test_supabase_repositories_construct_real_app_and_serve_inventory(tmp_path) -> None:
    client, inventory = _repository()
    locations = SupabaseLocationRepository(client, workspace_id="workspace-1")
    item_id = inventory.upsert_item(name="M4 T-nuts")
    inventory.put_inventory(
        item_id=item_id,
        bin_id="B001",
        quantity=12,
        unit="pieces",
    )
    photo = inventory.add_photo(
        filename="t-nuts.jpeg",
        content_type="image/jpeg",
        data=b"cloud-photo",
    )
    inventory.attach_photo(item_id=item_id, photo_id=photo.id)
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps(
            {
                "name": "supabase-test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]}
                ],
            }
        ),
        encoding="utf-8",
    )
    config = LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout),
        database_path=str(tmp_path / "must-not-exist.db"),
    )
    repositories = StorageRepositories(
        inventory=inventory,
        locations=locations,
        backend="supabase",
        workspace_id="workspace-1",
    )

    api = TestClient(create_app(config, repositories=repositories))

    assert api.get("/inventory").json()["items"][0]["name"] == "M4 T-nuts"
    photo_response = api.get(f"/photos/{photo.id}")
    assert photo_response.status_code == 200
    assert photo_response.content == b"cloud-photo"
    assert photo_response.headers["content-type"] == "image/jpeg"
    assert api.get("/health").json()["storage"] == {
        "backend": "supabase",
        "workspace_id": "workspace-1",
    }
    assert locations.count() == 3
    assert not (tmp_path / "must-not-exist.db").exists()


def test_supabase_api_uses_atomic_photo_putaway(tmp_path) -> None:
    client, inventory = _repository()
    locations = SupabaseLocationRepository(client, workspace_id="workspace-1")
    photo = inventory.add_photo(
        filename="blue-tool.jpeg",
        content_type="image/jpeg",
        data=b"photo",
    )
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps(
            {
                "name": "supabase-test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]}
                ],
            }
        ),
        encoding="utf-8",
    )
    repositories = StorageRepositories(
        inventory=inventory,
        locations=locations,
        backend="supabase",
        workspace_id="workspace-1",
    )
    api = TestClient(
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

    response = api.post(
        "/assistant/confirm",
        json={
            "command": "put away blue pry tool",
            "photo_ids": [photo.id],
            "plan": [
                {
                    "tool": "put_item",
                    "arguments": {
                        "item": "Blue pry tool",
                        "location_id": "B001",
                        "quantity": 1,
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"][0]["stored"] == "Blue pry tool"
    assert body["photo_attachments"]["item_links"] == [
        {"photo_id": photo.id, "item_id": body["applied"][0]["item_id"]}
    ]
    assert body["photo_attachments"]["review_links"] == []
    assert inventory.list_inventory()[0].photos == [f"/photos/{photo.id}"]
