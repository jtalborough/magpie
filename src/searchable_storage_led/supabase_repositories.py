from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from time import monotonic
from uuid import uuid4

from .layout import normalize_inventory_location_id
from .item_identity import identities_conflict
from .locations import (
    HardwareBinding,
    Location,
    LocationNameConflictError,
    normalize_binding_kind,
    normalize_internal_dimensions,
    pixels_from_binding,
)
from .store import (
    InventoryAuditConflictError,
    InventoryCatalogPage,
    LearnedRule,
    LearnedSizeRule,
    PhotoRecord,
    SearchResult,
    _placement_rule_conflicts,
    _size_rule_conflicts,
    clamp_confidence,
    inventory_catalog_page_from_results,
    normalize_rule_terms,
    normalize_size_class,
    suffix_for_content_type,
)
from .supabase_client import SupabaseError, SupabaseRestClient


LOCATION_COLUMNS = (
    "id,local_code,parent_id,type,label,grid,cell_col,cell_row,cell_w,cell_h,"
    "light_target,light_indexes,capacity_class,purpose_tags,placement_notes,internal_dimensions,"
    "accepts_items,active,archived_at,archive_group"
)
LOCATION_CACHE_TTL_SECONDS = 60.0
INVENTORY_CACHE_TTL_SECONDS = 60.0
CATALOG_RPC_RETRY_SECONDS = 60.0
ACTIVE_SIBLING_NAME_CONFLICT = (
    "an active storage place with that name already exists at this level"
)


class SupabaseLocationRepository:
    def __init__(self, client: SupabaseRestClient, *, workspace_id: str) -> None:
        clean_workspace = workspace_id.strip()
        if not clean_workspace:
            raise ValueError("Supabase workspace_id is required")
        self.client = client
        self.workspace_id = clean_workspace
        self._cached_rows: list[dict[str, object]] | None = None
        self._cache_time = 0.0
        self._latest_events_rpc_unavailable_until = 0.0

    def health_probe(self) -> None:
        self.client.select(
            "locations",
            columns="id",
            filters={"workspace_id": f"eq.{self.workspace_id}"},
            limit=1,
        )

    def invalidate_cache(self) -> None:
        self._cached_rows = None
        self._cache_time = 0.0

    def upsert(self, loc: Location) -> None:
        locations = self._location_index()
        locations_by_uuid = {str(row["id"]): row for row in locations.values()}
        parent_uuid = None
        if loc.parent_id == loc.id:
            raise ValueError("location cannot be its own parent")
        if loc.parent_id is not None:
            parent = locations.get(loc.parent_id)
            if parent is None:
                raise KeyError(f"unknown parent: {loc.parent_id}")
            if loc.active and not bool(parent.get("active", True)):
                raise ValueError(f"parent location is archived: {loc.parent_id}")
            seen: set[str] = set()
            current = parent
            while current is not None:
                current_code = str(current["local_code"])
                if current_code == loc.id:
                    raise ValueError("location cannot move inside its own subtree")
                if current_code in seen:
                    break
                seen.add(current_code)
                parent_id = current.get("parent_id")
                current = (
                    locations_by_uuid.get(str(parent_id))
                    if parent_id is not None
                    else None
                )
            parent_uuid = str(parent["id"])
        try:
            rows = self.client.upsert(
                "locations",
                {
                    "workspace_id": self.workspace_id,
                    "local_code": loc.id,
                    "parent_id": parent_uuid,
                    "type": loc.type,
                    "label": loc.label,
                    "grid": loc.grid or {},
                    "cell_col": loc.col,
                    "cell_row": loc.row,
                    "cell_w": loc.w,
                    "cell_h": loc.h,
                    "light_target": loc.light_target,
                    "light_indexes": list(loc.light_indexes),
                    "capacity_class": loc.capacity_class,
                    "purpose_tags": list(loc.purpose_tags),
                    "placement_notes": loc.placement_notes,
                    "internal_dimensions": dict(loc.internal_dimensions),
                    "accepts_items": loc.accepts_items,
                    "active": loc.active,
                    "archived_at": loc.archived_at,
                    "archive_group": (
                        None if loc.active else (loc.archive_group or loc.id)
                    ),
                },
                on_conflict="workspace_id,local_code",
            )
        except SupabaseError as exc:
            self._raise_location_write_error(exc)
            raise
        if rows:
            cached = [
                row
                for row in (self._cached_rows or [])
                if row.get("local_code") != loc.id
            ]
            cached.append(rows[0])
            self._cached_rows = cached
            self._cache_time = monotonic()

    def create_many(self, rows: list[Location]) -> list[str]:
        if not rows:
            return []
        ids = [location.id for location in rows]
        if len(ids) != len(set(ids)):
            raise ValueError("location IDs in one structure must be unique")
        existing = self._location_index()
        conflicts = [location_id for location_id in ids if location_id in existing]
        if conflicts:
            raise ValueError(f"location already exists: {conflicts[0]}")
        payload = [
            {
                "local_code": location.id,
                "parent_code": location.parent_id,
                "type": location.type,
                "label": location.label or location.id,
                "grid": location.grid or {},
                "cell_col": location.col,
                "cell_row": location.row,
                "cell_w": location.w,
                "cell_h": location.h,
                "light_target": location.light_target,
                "light_indexes": list(location.light_indexes),
                "capacity_class": location.capacity_class,
                "purpose_tags": list(location.purpose_tags),
                "placement_notes": location.placement_notes,
                "internal_dimensions": dict(location.internal_dimensions),
                "accepts_items": location.accepts_items,
            }
            for location in rows
        ]
        try:
            result = self.client.rpc(
                "magpie_create_locations",
                {
                    "target_workspace_id": self.workspace_id,
                    "location_rows": payload,
                },
            )
        except SupabaseError as exc:
            self._raise_location_write_error(exc)
            raise
        if not isinstance(result, dict) or result.get("location_ids") != ids:
            raise RuntimeError("Supabase location creation returned an invalid response")
        self._cached_rows = None
        self._cache_time = 0.0
        return ids

    def get(self, location_id: str, *, include_archived: bool = False) -> Location | None:
        return {
            loc.id: loc for loc in self.all(include_archived=include_archived)
        }.get(location_id)

    def get_any(self, location_id: str) -> Location | None:
        return self.get(location_id, include_archived=True)

    def children(
        self, parent_id: str | None, *, include_archived: bool = False
    ) -> list[Location]:
        return sorted(
            (
                loc
                for loc in self.all(include_archived=include_archived)
                if loc.parent_id == parent_id
            ),
            key=lambda loc: loc.id,
        )

    def all(self, *, include_archived: bool = False) -> list[Location]:
        rows = self._rows()
        codes_by_uuid = {str(row["id"]): str(row["local_code"]) for row in rows}
        return sorted(
            (
                self._location_from_row(row, codes_by_uuid)
                for row in rows
                if include_archived or bool(row.get("active", True))
            ),
            key=lambda loc: loc.id,
        )

    def count(self, *, include_archived: bool = False) -> int:
        return len(self.all(include_archived=include_archived))

    def ancestors(self, location_id: str) -> list[Location]:
        locations = {loc.id: loc for loc in self.all()}
        chain: list[Location] = []
        seen: set[str] = set()
        current = locations.get(location_id)
        while current is not None and current.id not in seen:
            chain.append(current)
            seen.add(current.id)
            current = locations.get(current.parent_id) if current.parent_id else None
        return chain

    def subtree_ids(
        self, location_id: str, *, include_archived: bool = False
    ) -> list[str]:
        locations = self.all(include_archived=include_archived)
        children: dict[str, list[str]] = {}
        for location in locations:
            if location.parent_id is not None:
                children.setdefault(location.parent_id, []).append(location.id)
        ids: list[str] = []
        queue = [location_id]
        while queue:
            current = queue.pop()
            ids.append(current)
            queue.extend(sorted(children.get(current, []), reverse=True))
        return ids

    def archive_subtree(self, location_id: str) -> list[str]:
        if self.get(location_id) is None:
            raise KeyError(f"unknown active location: {location_id}")
        return self._set_subtree_active(location_id, active=False)

    def restore_subtree(self, location_id: str) -> list[str]:
        location = self.get_any(location_id)
        if location is None:
            raise KeyError(f"unknown location: {location_id}")
        if location.parent_id:
            parent = self.get_any(location.parent_id)
            if parent is not None and not parent.active:
                raise ValueError("restore the parent location first")
        return self._set_subtree_active(location_id, active=True)

    def _set_subtree_active(self, location_id: str, *, active: bool) -> list[str]:
        try:
            result = self.client.rpc(
                "magpie_set_location_subtree_active",
                {
                    "target_workspace_id": self.workspace_id,
                    "root_location_code": location_id,
                    "target_active": active,
                },
            )
        except SupabaseError as exc:
            self._raise_location_write_error(exc)
            if "restore the parent location first" in str(exc).lower():
                raise ValueError("restore the parent location first") from exc
            raise
        if not isinstance(result, dict) or not isinstance(
            result.get("location_ids"), list
        ):
            raise RuntimeError("Supabase location lifecycle returned an invalid response")
        ids = [str(value) for value in result["location_ids"] if str(value)]
        if not ids or ids[0] != location_id:
            raise RuntimeError("Supabase location lifecycle returned an invalid subtree")
        self._cached_rows = None
        self._cache_time = 0.0
        return ids

    def delete_subtree(self, location_id: str) -> list[str]:
        ids = self.subtree_ids(location_id, include_archived=True)
        remote = self._location_index()
        for local_id in reversed(ids):
            row = remote.get(local_id)
            if row is not None:
                self.client.delete(
                    "locations",
                    filters=self._filters(id=f"eq.{row['id']}"),
                    return_rows=False,
                )
        removed = set(ids)
        self._cached_rows = [
            row
            for row in (self._cached_rows or [])
            if str(row.get("local_code")) not in removed
        ]
        self._cache_time = monotonic()
        return ids

    @staticmethod
    def _raise_location_write_error(exc: SupabaseError) -> None:
        if ACTIVE_SIBLING_NAME_CONFLICT in str(exc).lower():
            raise LocationNameConflictError(
                "A storage place with that name already exists at this level."
            ) from exc

    def move(self, location_id: str, new_parent_id: str | None) -> Location:
        location = self.get(location_id)
        if location is None:
            raise KeyError(f"unknown location: {location_id}")
        if new_parent_id is not None and self.get(new_parent_id) is None:
            raise KeyError(f"unknown parent: {new_parent_id}")
        if new_parent_id == location_id:
            raise ValueError("location cannot be its own parent")
        if new_parent_id is not None and new_parent_id in self.subtree_ids(location_id):
            raise ValueError("location cannot move inside its own subtree")
        moved = replace(location, parent_id=new_parent_id)
        self.upsert(moved)
        return moved

    def record_event(
        self,
        *,
        event_type: str,
        location_id: str,
        note: str = "",
        quantity: float | None = None,
    ) -> None:
        clean_type = event_type.strip()
        if not clean_type:
            raise ValueError("event_type is required")
        row = self._location_index().get(location_id)
        self.client.insert(
            "repository_events",
            {
                "workspace_id": self.workspace_id,
                "event_type": clean_type,
                "location_id": str(row["id"]) if row is not None else None,
                "location_code": location_id,
                "quantity": quantity,
                "note": note.strip(),
            },
            return_rows=False,
        )

    def events(self, location_id: str, *, limit: int = 25) -> list[dict[str, object]]:
        rows = self.client.select(
            "repository_events",
            columns="local_id,event_type,item_local_id,location_code,quantity,note,created_at",
            filters=self._filters(location_code=f"eq.{location_id}"),
            order="local_id.desc",
            limit=max(1, min(int(limit), 100)),
        )
        return [
            {
                "id": int(row["local_id"]),
                "event_type": str(row["event_type"]),
                "item_id": (
                    int(row["item_local_id"])
                    if row.get("item_local_id") is not None
                    else None
                ),
                "location_id": str(row.get("location_code") or ""),
                "quantity": (
                    float(row["quantity"]) if row.get("quantity") is not None else None
                ),
                "note": str(row.get("note") or ""),
                "created_at": row.get("created_at"),
            }
            for row in rows
        ]

    def latest_event(
        self,
        event_type: str,
        *,
        location_id: str,
    ) -> dict[str, object] | None:
        clean_type = event_type.strip()
        if not clean_type or not location_id:
            return None
        rows = self.client.select(
            "repository_events",
            columns="local_id,event_type,item_local_id,location_code,quantity,note,created_at",
            filters=self._filters(
                event_type=f"eq.{clean_type}",
                location_code=f"eq.{location_id}",
            ),
            order="local_id.desc",
            limit=1,
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "id": int(row["local_id"]),
            "event_type": str(row["event_type"]),
            "item_id": (
                int(row["item_local_id"])
                if row.get("item_local_id") is not None
                else None
            ),
            "location_id": str(row.get("location_code") or ""),
            "quantity": (
                float(row["quantity"]) if row.get("quantity") is not None else None
            ),
            "note": str(row.get("note") or ""),
            "created_at": row.get("created_at"),
        }

    def latest_events_by_location(
        self,
        event_type: str,
        *,
        limit: int = 5000,
    ) -> tuple[dict[str, dict[str, object]], bool]:
        clean_type = event_type.strip()
        if not clean_type:
            return {}, False
        clean_limit = max(1, min(int(limit), 5000))
        rows: list[dict[str, object]]
        truncated = False
        if (
            isinstance(self.client, SupabaseRestClient)
            and monotonic() >= self._latest_events_rpc_unavailable_until
        ):
            try:
                result = self.client.rpc(
                    "magpie_latest_location_events",
                    {
                        "target_workspace_id": self.workspace_id,
                        "target_event_type": clean_type,
                    },
                )
                rows = [
                    dict(row)
                    for row in result
                    if isinstance(row, dict)
                ] if isinstance(result, list) else []
                rows.sort(key=lambda row: int(row.get("local_id") or 0), reverse=True)
                if len(rows) > clean_limit:
                    rows = rows[:clean_limit]
                    truncated = True
            except SupabaseError:
                self._latest_events_rpc_unavailable_until = monotonic() + 60.0
                rows = []
            else:
                return self._latest_event_rows(rows), truncated
        rows = self.client.select(
            "repository_events",
            columns="local_id,event_type,item_local_id,location_code,quantity,note,created_at",
            filters=self._filters(event_type=f"eq.{clean_type}"),
            order="local_id.desc",
            limit=clean_limit + 1,
        )
        truncated = len(rows) > clean_limit
        return self._latest_event_rows(rows[:clean_limit]), truncated

    @staticmethod
    def _latest_event_rows(
        rows: list[dict[str, object]],
    ) -> dict[str, dict[str, object]]:
        events: dict[str, dict[str, object]] = {}
        for row in rows:
            location_id = str(row.get("location_code") or "")
            if not location_id or location_id in events:
                continue
            events[location_id] = {
                "id": int(row["local_id"]),
                "event_type": str(row["event_type"]),
                "item_id": (
                    int(row["item_local_id"])
                    if row.get("item_local_id") is not None
                    else None
                ),
                "location_id": location_id,
                "quantity": (
                    float(row["quantity"])
                    if row.get("quantity") is not None
                    else None
                ),
                "note": str(row.get("note") or ""),
                "created_at": row.get("created_at"),
            }
        return events

    def has_event(self, event_type: str, *, location_id: str = "") -> bool:
        clean_type = event_type.strip()
        if not clean_type:
            return False
        filters = self._filters(event_type=f"eq.{clean_type}")
        if location_id:
            filters["location_code"] = f"eq.{location_id}"
        rows = self.client.select(
            "repository_events",
            columns="local_id",
            filters=filters,
            order="local_id.desc",
            limit=1,
        )
        return bool(rows)

    def nearest_lit(self, location_id: str) -> Location | None:
        return next((loc for loc in self.ancestors(location_id) if loc.is_lit), None)

    def hardware_bindings(self, location_id: str) -> list[HardwareBinding]:
        location = self._location_index().get(location_id)
        if location is None:
            return []
        rows = self.client.select(
            "hardware_bindings",
            columns="local_id,kind,controller_id,address,metadata,active",
            filters=self._filters(location_id=f"eq.{location['id']}"),
            order="active.desc,kind.asc,local_id.asc",
        )
        return [self._binding_from_row(row, location_id) for row in rows]

    def hardware_bindings_by_location(
        self, location_ids: list[str]
    ) -> dict[str, list[HardwareBinding]]:
        wanted = {str(location_id) for location_id in location_ids if str(location_id)}
        if not wanted:
            return {}
        location_index = self._location_index()
        codes_by_uuid = {
            str(row["id"]): local_code
            for local_code, row in location_index.items()
            if local_code in wanted
        }
        if not codes_by_uuid:
            return {}
        rows = self.client.select(
            "hardware_bindings",
            columns="local_id,location_id,kind,controller_id,address,metadata,active",
            filters=self._filters(),
            order="active.desc,kind.asc,local_id.asc",
        )
        grouped: dict[str, list[HardwareBinding]] = {}
        for row in rows:
            location_id = codes_by_uuid.get(str(row.get("location_id") or ""))
            if location_id:
                grouped.setdefault(location_id, []).append(
                    self._binding_from_row(row, location_id)
                )
        return grouped

    def set_hardware_binding(
        self,
        *,
        location_id: str,
        kind: str,
        controller_id: str = "",
        address: str = "",
        metadata: dict | None = None,
        active: bool = True,
    ) -> HardwareBinding:
        location = self._location_index().get(location_id)
        if location is None:
            raise KeyError(f"unknown location: {location_id}")
        clean_kind = normalize_binding_kind(kind)
        if not clean_kind:
            raise ValueError("unsupported hardware binding kind")
        clean_controller = controller_id.strip()
        clean_address = address.strip()
        data = metadata or {}
        rows = self.client.upsert(
            "hardware_bindings",
            {
                "workspace_id": self.workspace_id,
                "location_id": str(location["id"]),
                "kind": clean_kind,
                "controller_id": clean_controller,
                "address": clean_address,
                "metadata": data,
                "active": bool(active),
            },
            on_conflict="workspace_id,location_id,kind,controller_id,address",
        )
        if not rows:
            raise RuntimeError("Supabase hardware binding upsert returned no row")
        if active:
            self.client.update(
                "hardware_bindings",
                {"active": False},
                filters=self._filters(
                    location_id=f"eq.{location['id']}",
                    kind=f"eq.{clean_kind}",
                    controller_id=f"eq.{clean_controller}",
                    address=f"neq.{clean_address}",
                ),
                return_rows=False,
            )
        if clean_kind == "wled_pixels":
            pixels = pixels_from_binding(clean_address, data)
            self.client.update(
                "locations",
                {
                    "light_target": clean_controller or None,
                    "light_indexes": pixels if active else [],
                },
                filters=self._filters(id=f"eq.{location['id']}"),
                return_rows=False,
            )
        return self._binding_from_row(rows[0], location_id)

    def set_hardware_bindings(
        self, rows: list[dict[str, object]]
    ) -> list[HardwareBinding]:
        if not rows:
            return []
        location_index = self._location_index()
        payload: list[dict[str, object]] = []
        identities: set[tuple[str, str, str, str]] = set()
        for row in rows:
            location_id = str(row.get("location_id") or "").strip()
            if location_id not in location_index:
                raise KeyError(f"unknown location: {location_id}")
            clean_kind = normalize_binding_kind(str(row.get("kind") or ""))
            if not clean_kind:
                raise ValueError("unsupported hardware binding kind")
            clean_controller = str(row.get("controller_id") or "").strip()
            clean_address = str(row.get("address") or "").strip()
            identity = (location_id, clean_kind, clean_controller, clean_address)
            if identity in identities:
                raise ValueError("duplicate hardware binding in one batch")
            identities.add(identity)
            payload.append(
                {
                    "location_code": location_id,
                    "kind": clean_kind,
                    "controller_id": clean_controller,
                    "address": clean_address,
                    "metadata": dict(row.get("metadata") or {}),
                    "active": bool(row.get("active", True)),
                }
            )
        result = self.client.rpc(
            "magpie_set_hardware_bindings",
            {
                "target_workspace_id": self.workspace_id,
                "binding_rows": payload,
            },
        )
        result_rows = result.get("bindings") if isinstance(result, dict) else None
        if not isinstance(result_rows, list) or len(result_rows) != len(payload):
            raise RuntimeError("Supabase hardware binding batch returned an invalid response")
        self._cached_rows = None
        self._cache_time = 0.0
        return [
            self._binding_from_row(binding, str(binding["location_code"]))
            for binding in result_rows
        ]

    def _rows(self) -> list[dict[str, object]]:
        if (
            self._cached_rows is not None
            and monotonic() - self._cache_time < LOCATION_CACHE_TTL_SECONDS
        ):
            return list(self._cached_rows)
        rows = self.client.select(
            "locations",
            columns=LOCATION_COLUMNS,
            filters=self._filters(),
            order="local_code.asc",
        )
        self._cached_rows = rows
        self._cache_time = monotonic()
        return list(rows)

    def _location_index(self) -> dict[str, dict[str, object]]:
        return {str(row["local_code"]): row for row in self._rows()}

    def _filters(self, **filters: str) -> dict[str, str]:
        return {"workspace_id": f"eq.{self.workspace_id}", **filters}

    @staticmethod
    def _location_from_row(
        row: dict[str, object],
        codes_by_uuid: dict[str, str],
    ) -> Location:
        raw_grid = row.get("grid")
        raw_lights = row.get("light_indexes")
        raw_tags = row.get("purpose_tags")
        raw_dimensions = row.get("internal_dimensions")
        parent_uuid = row.get("parent_id")
        return Location(
            id=str(row["local_code"]),
            type=str(row["type"]),
            parent_id=codes_by_uuid.get(str(parent_uuid)) if parent_uuid else None,
            label=str(row.get("label") or ""),
            grid=dict(raw_grid) if isinstance(raw_grid, dict) and raw_grid else None,
            col=int(row["cell_col"]) if row.get("cell_col") is not None else None,
            row=int(row["cell_row"]) if row.get("cell_row") is not None else None,
            w=int(row.get("cell_w") or 1),
            h=int(row.get("cell_h") or 1),
            light_target=str(row["light_target"]) if row.get("light_target") else None,
            light_indexes=(
                [int(index) for index in raw_lights]
                if isinstance(raw_lights, list)
                else []
            ),
            capacity_class=str(row.get("capacity_class") or "medium"),
            purpose_tags=(
                [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else []
            ),
            placement_notes=str(row.get("placement_notes") or ""),
            internal_dimensions=normalize_internal_dimensions(raw_dimensions),
            accepts_items=bool(row.get("accepts_items", True)),
            active=bool(row.get("active", True)),
            archived_at=str(row["archived_at"]) if row.get("archived_at") else None,
            archive_group=(
                str(row["archive_group"]) if row.get("archive_group") else None
            ),
        )

    @staticmethod
    def _binding_from_row(row: dict[str, object], location_id: str) -> HardwareBinding:
        metadata = row.get("metadata")
        return HardwareBinding(
            id=int(row["local_id"]),
            location_id=location_id,
            kind=str(row["kind"]),
            controller_id=str(row.get("controller_id") or ""),
            address=str(row.get("address") or ""),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
            active=bool(row.get("active", True)),
        )


class SupabaseInventoryRepository:
    def __init__(
        self,
        client: SupabaseRestClient,
        *,
        workspace_id: str,
        photo_bucket: str = "magpie-photos",
    ) -> None:
        clean_workspace = workspace_id.strip()
        if not clean_workspace:
            raise ValueError("Supabase workspace_id is required")
        if not photo_bucket.strip():
            raise ValueError("Supabase photo bucket is required")
        self.client = client
        self.workspace_id = clean_workspace
        self.photo_bucket = photo_bucket.strip()
        self._inventory_cache: list[SearchResult] | None = None
        self._inventory_cache_time = 0.0
        self._location_count_cache: dict[str, int] | None = None
        self._location_count_cache_time = 0.0
        self._catalog_rpc_retry_after = 0.0
        self._inventory_cache_lock = RLock()

    def upsert_item(
        self,
        *,
        name: str,
        description: str = "",
        category: str = "",
        aliases: list[str] | None = None,
        photo_ids: list[int] | None = None,
    ) -> int:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("item name is required")
        existing = self.client.select(
            "items",
            filters=self._filters(name=f"eq.{clean_name}"),
            limit=1,
        )
        if existing and existing[0].get("merged_into_item_id"):
            target_local_id = self.resolve_item_id(int(existing[0]["local_id"]))
            target = self._item(target_local_id) if target_local_id is not None else None
            if target is None:
                raise RuntimeError("merged item target was not found")
            item_id = int(target["local_id"])
            for alias in [clean_name, *(aliases or [])]:
                clean_alias = alias.strip()
                if clean_alias:
                    self.client.upsert(
                        "item_aliases",
                        {"item_id": str(target["id"]), "alias": clean_alias},
                        on_conflict="item_id,alias",
                        return_rows=False,
                    )
            for photo_id in photo_ids or []:
                self.attach_photo(item_id=item_id, photo_id=int(photo_id))
            self._record_event("item_upsert", item=target, note=clean_name)
            return item_id
        rows = self.client.upsert(
            "items",
            {
                "workspace_id": self.workspace_id,
                "name": clean_name,
                "description": description.strip(),
                "category": category.strip(),
                "active": True,
                "archived_at": None,
            },
            on_conflict="workspace_id,name",
        )
        if not rows:
            raise RuntimeError("Supabase item upsert returned no row")
        item = rows[0]
        item_id = int(item["local_id"])
        item_uuid = str(item["id"])
        for alias in aliases or []:
            clean_alias = alias.strip()
            if clean_alias:
                self.client.upsert(
                    "item_aliases",
                    {"item_id": item_uuid, "alias": clean_alias},
                    on_conflict="item_id,alias",
                    return_rows=False,
                )
        for photo_id in photo_ids or []:
            self.attach_photo(item_id=item_id, photo_id=int(photo_id))
        self._record_event("item_upsert", item=item, note=clean_name)
        self._invalidate_inventory_cache()
        return item_id

    def archive_item(self, item_id: int) -> bool:
        item = self._item(item_id)
        if item is None or not bool(item.get("active", True)):
            return False
        rows = self.client.update(
            "items",
            {"active": False, "archived_at": datetime.now(UTC).isoformat()},
            filters=self._filters(local_id=f"eq.{int(item_id)}", active="eq.true"),
        )
        if not rows:
            return False
        self._record_event("item_archived", item=item, note=str(item["name"]))
        self._invalidate_inventory_cache()
        return True

    def restore_item(self, item_id: int) -> bool:
        item = self._item(item_id)
        if (
            item is None
            or bool(item.get("active", True))
            or item.get("merged_into_item_id")
        ):
            return False
        rows = self.client.update(
            "items",
            {"active": True, "archived_at": None},
            filters=self._filters(local_id=f"eq.{int(item_id)}", active="eq.false"),
        )
        if not rows:
            return False
        self._record_event("item_restored", item=item, note=str(item["name"]))
        self._invalidate_inventory_cache()
        return True

    def resolve_item_id(self, item_id: int) -> int | None:
        item = self._item(item_id)
        seen: set[str] = set()
        while item is not None:
            item_uuid = str(item["id"])
            if item_uuid in seen:
                return None
            seen.add(item_uuid)
            merged_into = item.get("merged_into_item_id")
            if not merged_into:
                return int(item["local_id"])
            rows = self.client.select(
                "items",
                filters=self._filters(id=f"eq.{merged_into}"),
                limit=1,
            )
            item = rows[0] if rows else None
        return None

    def item_merge_preview(
        self, *, source_item_id: int, target_item_id: int
    ) -> dict[str, object]:
        source_id = int(source_item_id)
        target_id = int(target_item_id)
        if source_id == target_id:
            raise ValueError("source and canonical item must be different")
        source = self._item(source_id)
        target = self._item(target_id)
        if source is None or target is None:
            raise ValueError("item not found")
        if not bool(source.get("active", True)) or source.get("merged_into_item_id"):
            raise ValueError("source item is not active")
        if not bool(target.get("active", True)) or target.get("merged_into_item_id"):
            raise ValueError("canonical item is not active")
        source_rows = {row.bin_id: row for row in self.item_inventory(source_id)}
        target_rows = {row.bin_id: row for row in self.item_inventory(target_id)}
        placements: list[dict[str, object]] = []
        conflicts: list[dict[str, object]] = []
        for location_id, source_row in source_rows.items():
            target_row = target_rows.get(location_id)
            if target_row is not None and target_row.unit.casefold() != source_row.unit.casefold():
                conflicts.append(
                    {
                        "location_id": location_id,
                        "source_unit": source_row.unit,
                        "target_unit": target_row.unit,
                    }
                )
                continue
            quantity = source_row.quantity + (target_row.quantity if target_row else 0)
            placements.append(
                {
                    "location_id": location_id,
                    "quantity": quantity,
                    "unit": target_row.unit if target_row else source_row.unit,
                    "combined": target_row is not None,
                }
            )
        aliases = self.client.select(
            "item_aliases", filters={"item_id": f"eq.{source['id']}"}
        )
        photos = self.client.select(
            "photos",
            filters=self._filters(
                object_type="eq.item", object_id=f"eq.{source['id']}"
            ),
        )
        return {
            "source": {"item_id": source_id, "name": str(source["name"])},
            "target": {"item_id": target_id, "name": str(target["name"])},
            "placements": placements,
            "conflicts": conflicts,
            "aliases_transferred": len(aliases) + 1,
            "photos_transferred": len(photos),
            "can_merge": not conflicts,
        }

    def merge_item(self, *, source_item_id: int, target_item_id: int) -> dict[str, object]:
        preview = self.item_merge_preview(
            source_item_id=source_item_id, target_item_id=target_item_id
        )
        if preview["conflicts"]:
            raise ValueError("items use different units in the same location")
        result = self.client.rpc(
            "magpie_merge_items",
            {
                "target_workspace_id": self.workspace_id,
                "source_item_local_id": int(source_item_id),
                "canonical_item_local_id": int(target_item_id),
            },
        )
        if not isinstance(result, dict) or not result.get("merged"):
            raise RuntimeError("Supabase item merge returned an invalid response")
        self._invalidate_inventory_cache()
        return preview

    def update_item(
        self,
        *,
        item_id: int,
        name: str,
        description: str = "",
        category: str = "",
        aliases: list[str] | None = None,
    ) -> None:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("item name is required")
        item = self._item(item_id)
        if item is None:
            raise ValueError("item not found")
        rows = self.client.update(
            "items",
            {
                "name": clean_name,
                "description": description.strip(),
                "category": category.strip(),
            },
            filters=self._filters(local_id=f"eq.{int(item_id)}"),
        )
        if not rows:
            raise ValueError("item not found")
        self.client.delete(
            "item_aliases",
            filters={"item_id": f"eq.{item['id']}"},
            return_rows=False,
        )
        for alias in aliases or []:
            clean_alias = alias.strip()
            if clean_alias:
                self.client.upsert(
                    "item_aliases",
                    {"item_id": str(item["id"]), "alias": clean_alias},
                    on_conflict="item_id,alias",
                    return_rows=False,
                )
        self._record_event("item_update", item=item, note=clean_name)

    def put_inventory(
        self,
        *,
        item_id: int,
        bin_id: str,
        quantity: float,
        unit: str,
        needs_review: bool = False,
        review_reason: str = "",
        normalize_bin: bool = True,
    ) -> None:
        location_code = (
            normalize_inventory_location_id(bin_id) if normalize_bin else bin_id
        )
        item = self._require_item(item_id)
        location = self._require_location(location_code)
        self.client.upsert(
            "inventory_entries",
            {
                "workspace_id": self.workspace_id,
                "item_id": str(item["id"]),
                "location_id": str(location["id"]),
                "quantity": float(quantity),
                "unit": unit.strip() or "each",
                "status": "review" if needs_review else "present",
                "needs_review": bool(needs_review),
                "review_reason": review_reason.strip() if needs_review else "",
            },
            on_conflict="workspace_id,item_id,location_id",
        )
        changes: dict[str, object] = {
            "last_seen_location_id": str(location["id"]),
            "active": True,
            "archived_at": None,
        }
        if not item.get("home_location_id"):
            changes["home_location_id"] = str(location["id"])
        self.client.update(
            "items",
            changes,
            filters=self._filters(local_id=f"eq.{int(item_id)}"),
            return_rows=False,
        )
        self._record_event(
            "inventory_put",
            item=item,
            location=location,
            quantity=float(quantity),
            note="needs_review" if needs_review else "",
        )
        self._invalidate_inventory_cache()

    def import_inventory_rows_atomic(
        self,
        *,
        new_locations: list[Location],
        rows: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        location_rows = [
            {
                "local_code": location.id,
                "parent_code": location.parent_id,
                "type": location.type,
                "label": location.label or location.id,
                "capacity_class": location.capacity_class,
                "accepts_items": location.accepts_items,
            }
            for location in new_locations
        ]
        inventory_rows = [
            {
                "name": str(row.get("name") or "").strip(),
                "location_code": str(row.get("location_id") or "").strip(),
                "quantity": float(row.get("quantity") or 0),
                "unit": str(row.get("unit") or "each").strip() or "each",
                "category": str(row.get("category") or "").strip(),
                "description": str(row.get("description") or "").strip(),
                "aliases": [
                    str(alias).strip()
                    for alias in list(row.get("aliases") or [])
                    if str(alias).strip()
                ],
            }
            for row in rows
        ]
        result = self.client.rpc(
            "magpie_import_inventory",
            {
                "target_workspace_id": self.workspace_id,
                "location_rows": location_rows,
                "inventory_rows": inventory_rows,
            },
        )
        if not isinstance(result, dict) or not isinstance(result.get("rows"), list):
            raise RuntimeError("Supabase inventory import returned an invalid response")
        imported = []
        for row in result["rows"]:
            if not isinstance(row, dict):
                raise RuntimeError("Supabase inventory import returned an invalid row")
            imported.append(
                {
                    "item_id": int(row["item_id"]),
                    "name": str(row["name"]),
                    "location_id": str(row["location_id"]),
                    "quantity": float(row["quantity"]),
                    "unit": str(row["unit"]),
                }
            )
        if len(imported) != len(inventory_rows):
            raise RuntimeError("Supabase inventory import returned the wrong row count")
        self._invalidate_inventory_cache()
        return imported

    def put_item_with_photos_atomic(
        self,
        *,
        item: str,
        location_id: str,
        quantity: float,
        unit: str,
        quantity_mode: str,
        photo_ids: list[int],
        needs_review: bool = False,
        review_reason: str = "",
    ) -> dict[str, object]:
        clean_photo_ids = list(dict.fromkeys(int(photo_id) for photo_id in photo_ids))
        if not clean_photo_ids:
            raise ValueError("at least one photo is required for atomic put-away")
        result = self.client.rpc(
            "magpie_put_item_with_photos",
            {
                "target_workspace_id": self.workspace_id,
                "target_item_name": item.strip(),
                "target_location_code": location_id,
                "requested_quantity": float(quantity),
                "requested_unit": unit.strip() or "each",
                "quantity_mode": quantity_mode.strip().lower() or "set",
                "target_photo_local_ids": clean_photo_ids,
                "mark_for_review": bool(needs_review),
                "target_review_reason": review_reason.strip(),
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError("Supabase atomic put-away returned an invalid response")
        self.restore_item(int(result["item_id"]))
        self._invalidate_inventory_cache()
        return {
            "stored": str(result["stored"]),
            "item_id": int(result["item_id"]),
            "location_id": str(result["location_id"]),
            "needs_review": bool(result.get("needs_review", False)),
            "previous_quantity": (
                float(result["previous_quantity"])
                if result.get("previous_quantity") is not None
                else None
            ),
            "previous_unit": result.get("previous_unit"),
            "quantity": float(result["quantity"]),
        }

    def search(self, query: str, *, limit: int = 20) -> list[SearchResult]:
        clean_query = " ".join(query.lower().split())[:200]
        terms = [term for term in clean_query.split() if term]
        if not terms:
            return []
        clean_limit = max(1, min(int(limit), 100))
        if isinstance(self.client, SupabaseRestClient):
            try:
                matching_ids: set[str] | None = None
                for term in terms:
                    term_ids = self._matching_item_uuids(term)
                    matching_ids = (
                        term_ids
                        if matching_ids is None
                        else matching_ids & term_ids
                    )
                    if not matching_ids:
                        return []
                assert matching_ids is not None
                entries, _ = self.client.select_page(
                    "inventory_entries",
                    columns=self._location_content_columns(),
                    filters=self._filters(
                        item_id=f"in.({','.join(sorted(matching_ids))})"
                    ),
                    order="item_id.asc,location_id.asc",
                    limit=clean_limit,
                    offset=0,
                )
                return sorted(
                    self._embedded_location_content_results(entries),
                    key=lambda result: (result.name, result.bin_id),
                )[:clean_limit]
            except SupabaseError:
                # Retain the cached compatibility path while PostgREST schema
                # relationships are rolling out or temporarily unavailable.
                pass
        results = [
            result
            for result in self.list_inventory()
            if all(
                any(
                    term in value.lower()
                    for value in [
                        result.name,
                        result.description,
                        result.category,
                        *result.aliases,
                    ]
                )
                for term in terms
            )
        ]
        return sorted(results, key=lambda result: (result.name, result.bin_id))[
            :clean_limit
        ]

    def list_inventory(self, *, include_archived: bool = False) -> list[SearchResult]:
        return sorted(
            (
                result
                for result in self._inventory_results()
                if include_archived or result.active
            ),
            key=lambda result: (result.bin_id, result.name),
        )

    def inventory_catalog_page(
        self,
        *,
        archived: bool = False,
        query: str = "",
        category: str = "",
        location_ids: set[str] | None = None,
        scope: str = "all",
        sort: str = "name",
        limit: int = 50,
        offset: int = 0,
    ) -> InventoryCatalogPage:
        clean_scope = scope.strip().lower() or "all"
        clean_sort = sort.strip().lower() or "name"
        if clean_scope not in {
            "all",
            "review",
            "photos",
            "no-photo",
            "uncategorized",
        }:
            raise ValueError("unknown inventory scope")
        if clean_sort not in {"name", "category", "location", "quantity"}:
            raise ValueError("unknown inventory sort")
        clean_limit = max(1, min(int(limit), 100))
        clean_offset = max(0, int(offset))
        clean_location_ids = {
            str(location_id).strip()
            for location_id in location_ids or set()
            if str(location_id).strip()
        }
        if (
            isinstance(self.client, SupabaseRestClient)
            and monotonic() >= self._catalog_rpc_retry_after
        ):
            try:
                payload = self.client.rpc(
                    "magpie_inventory_catalog_page_v2",
                    {
                        "target_workspace_id": self.workspace_id,
                        "include_archived": bool(archived),
                        "search_query": " ".join(query.split())[:200],
                        "category_filter": category.strip()[:100],
                        "scope_filter": clean_scope,
                        "sort_mode": clean_sort,
                        "page_limit": clean_limit,
                        "page_offset": clean_offset,
                        "location_filters": sorted(clean_location_ids),
                    },
                )
                if not isinstance(payload, dict):
                    raise SupabaseError(
                        "Supabase inventory catalog returned an invalid response"
                    )
                raw_items = payload.get("items")
                raw_metrics = payload.get("metrics")
                raw_categories = payload.get("categories")
                if (
                    not isinstance(raw_items, list)
                    or not isinstance(raw_metrics, dict)
                    or not isinstance(raw_categories, list)
                ):
                    raise SupabaseError(
                        "Supabase inventory catalog returned an invalid response"
                    )
                items: list[SearchResult] = []
                for raw in raw_items:
                    if not isinstance(raw, dict):
                        continue
                    photo_ids = raw.get("photo_ids")
                    items.append(
                        SearchResult(
                            item_id=int(raw["item_id"]),
                            name=str(raw["name"]),
                            description=str(raw.get("description") or ""),
                            category=str(raw.get("category") or ""),
                            aliases=[
                                str(alias)
                                for alias in raw.get("aliases", [])
                                if str(alias)
                            ],
                            bin_id=str(raw["bin_id"]),
                            quantity=float(raw.get("quantity") or 0),
                            unit=str(raw.get("unit") or "each"),
                            needs_review=bool(raw.get("needs_review")),
                            review_reason=str(raw.get("review_reason") or ""),
                            photos=[
                                f"/photos/{int(photo_id)}"
                                for photo_id in photo_ids or []
                            ],
                            size_class=str(raw.get("size_class") or ""),
                            size_source=str(raw.get("size_source") or ""),
                            size_confidence=float(raw.get("size_confidence") or 0),
                            dimensions=(
                                dict(raw["dimensions"])
                                if isinstance(raw.get("dimensions"), dict)
                                and raw.get("dimensions")
                                else None
                            ),
                            home_location_id=str(
                                raw.get("home_location_id") or ""
                            ),
                            last_seen_location_id=str(
                                raw.get("last_seen_location_id") or ""
                            ),
                            active=bool(raw.get("active", True)),
                            archived_at=(
                                str(raw["archived_at"])
                                if raw.get("archived_at")
                                else None
                            ),
                        )
                    )
                metrics = {
                    key: int(raw_metrics.get(key) or 0)
                    for key in ("items", "placements", "locations", "need_review")
                }
                return InventoryCatalogPage(
                    items=items,
                    total=max(0, int(payload.get("total") or 0)),
                    metrics=metrics,
                    categories=[
                        str(value) for value in raw_categories if str(value).strip()
                    ],
                )
            except (KeyError, TypeError, ValueError, SupabaseError):
                # Preserve hosted workspaces while the catalog RPC migration
                # reaches an older Supabase project without retrying it for
                # every browser page request.
                self._catalog_rpc_retry_after = (
                    monotonic() + CATALOG_RPC_RETRY_SECONDS
                )
        return inventory_catalog_page_from_results(
            self.list_inventory(include_archived=True),
            archived=archived,
            query=query,
            category=category,
            location_ids=clean_location_ids,
            scope=clean_scope,
            sort=clean_sort,
            limit=clean_limit,
            offset=clean_offset,
        )

    def bin_inventory(self, bin_id: str) -> list[SearchResult]:
        normalized = normalize_inventory_location_id(bin_id)
        return sorted(
            (
                result
                for result in self._inventory_results()
                if result.active and result.bin_id == normalized
            ),
            key=lambda result: result.name,
        )

    def review_queue(self, *, limit: int = 100) -> list[SearchResult]:
        clean_limit = max(1, min(int(limit), 500))
        try:
            entries = self.client.select(
                "inventory_entries",
                columns=self._location_content_columns(),
                filters=self._filters(
                    needs_review="eq.true",
                    **{"items.active": "eq.true"},
                ),
                order="updated_at.desc",
                limit=clean_limit,
            )
            results = self._embedded_location_content_results(entries)
            if len(results) == len(entries):
                return results
        except (KeyError, TypeError, ValueError, SupabaseError):
            pass
        return [
            result
            for result in self._inventory_results()
            if result.active and result.needs_review
        ][:clean_limit]

    def mark_reviewed(self, *, item_id: int, bin_id: str) -> None:
        clean_location = bin_id.strip()
        entry, item, location = self._entry(item_id, clean_location)
        normalized_location = normalize_inventory_location_id(clean_location)
        if entry is None and clean_location != normalized_location:
            entry, item, location = self._entry(
                item_id, normalized_location
            )
        if entry is not None:
            self.client.update(
                "inventory_entries",
                {"needs_review": False, "review_reason": "", "status": "present"},
                filters=self._filters(id=f"eq.{entry['id']}"),
                return_rows=False,
            )
        self._record_event("inventory_reviewed", item=item, location=location)

    def mark_needs_review(self, *, item_id: int, bin_id: str, reason: str = "") -> None:
        entry, item, location = self._entry(item_id, bin_id)
        if entry is not None:
            self.client.update(
                "inventory_entries",
                {
                    "needs_review": True,
                    "review_reason": reason.strip(),
                    "status": "review",
                },
                filters=self._filters(id=f"eq.{entry['id']}"),
                return_rows=False,
            )
        self._record_event("inventory_needs_review", item=item, location=location)

    def apply_location_audit(
        self,
        *,
        location_id: str,
        location_ids: list[str],
        placements: list[dict[str, object]],
        verification_note: str,
    ) -> list[dict[str, object]]:
        del location_ids
        if len(placements) > 100:
            raise ValueError("a contents check can include at most 100 placements")
        try:
            result = self.client.rpc(
                "magpie_apply_location_audit",
                {
                    "target_workspace_id": self.workspace_id,
                    "root_location_code": location_id,
                    "audit_rows": placements,
                    "verification_note": verification_note.strip(),
                },
            )
        except SupabaseError as exc:
            if "inventory changed while this contents check was open" in str(exc).lower():
                raise InventoryAuditConflictError(
                    "inventory changed while this contents check was open"
                ) from exc
            raise
        if not isinstance(result, dict) or not isinstance(result.get("updates"), list):
            raise RuntimeError("Supabase contents check returned an invalid response")
        self._invalidate_inventory_cache()
        return [dict(row) for row in result["updates"] if isinstance(row, dict)]

    def link_review_photo(self, *, item_id: int, bin_id: str, photo_id: int) -> None:
        entry, _, _ = self._entry(item_id, bin_id)
        photo = self._photo(photo_id)
        if entry is None or photo is None:
            return
        self.client.upsert(
            "inventory_review_photos",
            {
                "workspace_id": self.workspace_id,
                "inventory_entry_id": str(entry["id"]),
                "photo_id": str(photo["id"]),
            },
            on_conflict="inventory_entry_id,photo_id",
            return_rows=False,
        )

    def clear_bin_inventory(self, bin_id: str) -> int:
        location = self._location(normalize_inventory_location_id(bin_id))
        if location is None:
            return 0
        rows = self.client.delete(
            "inventory_entries",
            filters=self._filters(location_id=f"eq.{location['id']}"),
        )
        removed = len(rows)
        self._record_event(
            "inventory_clear_bin",
            location=location,
            quantity=float(removed),
        )
        return removed

    def remove_inventory(
        self,
        *,
        item_id: int,
        bin_id: str,
        normalize_bin: bool = False,
        note: str = "",
    ) -> bool:
        location_code = (
            normalize_inventory_location_id(bin_id) if normalize_bin else bin_id
        )
        entry, item, location = self._entry(item_id, location_code)
        if entry is None:
            return False
        self.client.delete(
            "inventory_entries",
            filters=self._filters(id=f"eq.{entry['id']}"),
            return_rows=False,
        )
        self._record_event(
            "inventory_remove",
            item=item,
            location=location,
            note=note.strip(),
        )
        return True

    def move_inventory(
        self,
        *,
        item_id: int,
        from_bin: str,
        to_bin: str,
        normalize_bin: bool = False,
    ) -> bool:
        source_code = (
            normalize_inventory_location_id(from_bin) if normalize_bin else from_bin
        )
        target_code = (
            normalize_inventory_location_id(to_bin) if normalize_bin else to_bin
        )
        if source_code == target_code:
            return False
        source, item, _ = self._entry(item_id, source_code)
        if source is None:
            return False
        target_location = self._require_location(target_code)
        target, _, _ = self._entry(item_id, target_code)
        quantity = float(source.get("quantity") or 0)
        if target is None:
            self.client.update(
                "inventory_entries",
                {"location_id": str(target_location["id"])},
                filters=self._filters(id=f"eq.{source['id']}"),
                return_rows=False,
            )
        else:
            self.client.update(
                "inventory_entries",
                {"quantity": float(target.get("quantity") or 0) + quantity},
                filters=self._filters(id=f"eq.{target['id']}"),
                return_rows=False,
            )
            self.client.delete(
                "inventory_entries",
                filters=self._filters(id=f"eq.{source['id']}"),
                return_rows=False,
            )
        changes: dict[str, object] = {"last_seen_location_id": str(target_location["id"])}
        if item is not None and not item.get("home_location_id"):
            changes["home_location_id"] = str(target_location["id"])
        self.client.update(
            "items",
            changes,
            filters=self._filters(local_id=f"eq.{int(item_id)}"),
            return_rows=False,
        )
        self._record_event(
            "inventory_move",
            item=item,
            location=target_location,
            quantity=quantity,
            note=f"from {source_code}",
        )
        return True

    def occupied_bin_ids(self) -> set[str]:
        return {result.bin_id for result in self._inventory_results() if result.active}

    def learn_alias(self, *, item_id: int, alias: str) -> None:
        clean = alias.strip()
        if not clean:
            return
        item = self._require_item(item_id)
        for match in self.exact_match(clean):
            if match.item_id != item_id:
                if identities_conflict(clean, match.name):
                    raise ValueError(
                        f'alias "{clean}" conflicts with distinct item {match.name}'
                    )
                raise ValueError(f'alias "{clean}" already identifies {match.name}')
        self.client.upsert(
            "item_aliases",
            {"item_id": str(item["id"]), "alias": clean},
            on_conflict="item_id,alias",
            return_rows=False,
        )
        self._record_event("alias_learned", item=item, note=clean)

    def remove_alias(self, *, item_id: int, alias: str) -> bool:
        clean = alias.strip()
        if not clean:
            return False
        item = self._item(item_id)
        if item is None:
            return False
        aliases = self.client.select(
            "item_aliases",
            filters={"item_id": f"eq.{item['id']}"},
        )
        match = next(
            (row for row in aliases if str(row.get("alias") or "").lower() == clean.lower()),
            None,
        )
        if match is None:
            return False
        self.client.delete(
            "item_aliases",
            filters={"item_id": f"eq.{item['id']}", "alias": f"eq.{match['alias']}"},
            return_rows=False,
        )
        self._record_event("alias_removed", item=item, note=clean)
        return True

    def exact_match(self, query: str) -> list[SearchResult]:
        needle = query.strip().lower()
        if not needle:
            return []
        for result in self._inventory_results():
            if result.active and (
                result.name.lower() == needle
                or needle in {alias.lower() for alias in result.aliases}
            ):
                return self.item_inventory(result.item_id)
        return []

    def item_inventory(self, item_id: int) -> list[SearchResult]:
        return sorted(
            (
                result
                for result in self._inventory_results()
                if result.active and result.item_id == item_id
            ),
            key=lambda result: result.bin_id,
        )

    def items_in_location(self, location_id: str) -> list[SearchResult]:
        return sorted(
            (
                result
                for result in self._inventory_results()
                if result.active and result.bin_id == location_id
            ),
            key=lambda result: result.name,
        )

    def items_in_locations(self, location_ids: list[str]) -> list[SearchResult]:
        wanted = {str(location_id) for location_id in location_ids}
        return sorted(
            (
                result
                for result in self._inventory_results()
                if result.active and result.bin_id in wanted
            ),
            key=lambda result: (result.bin_id, result.name),
        )

    def items_in_locations_page(
        self,
        location_ids: list[str],
        *,
        query: str = "",
        limit: int = 30,
        offset: int = 0,
    ) -> tuple[list[SearchResult], int]:
        wanted = {str(location_id) for location_id in location_ids if str(location_id)}
        if not wanted:
            return [], 0
        clean_query = " ".join(query.lower().split())[:200]
        clean_limit = max(1, min(int(limit), 100))
        clean_offset = max(0, int(offset))
        if not isinstance(self.client, SupabaseRestClient):
            matches = [
                result
                for result in self.items_in_locations(sorted(wanted))
                if not clean_query
                or clean_query
                in " ".join(
                    [
                        result.name,
                        result.category,
                        result.description,
                        result.bin_id,
                        *result.aliases,
                    ]
                ).lower()
            ]
            return matches[clean_offset : clean_offset + clean_limit], len(matches)

        try:
            item_ids: set[str] | None = None
            if clean_query:
                item_ids = self._matching_item_uuids(clean_query)
                if not item_ids:
                    return [], 0
            filters = self._filters(
                **{
                    "locations.local_code": self._postgrest_in(sorted(wanted)),
                    "items.active": "eq.true",
                },
            )
            if item_ids is not None:
                filters["item_id"] = f"in.({','.join(sorted(item_ids))})"
            entries, total = self.client.select_page(
                "inventory_entries",
                columns=self._location_content_columns(),
                filters=filters,
                order="location_id.asc,item_id.asc",
                limit=clean_limit,
                offset=clean_offset,
            )
            return self._embedded_location_content_results(entries), total
        except SupabaseError:
            # Older PostgREST schema caches may briefly miss a newly exposed
            # relationship. Keep the bounded multi-query path available during
            # that rollout window.
            pass

        if clean_query:
            matches = [
                result
                for result in self.items_in_locations(sorted(wanted))
                if clean_query
                in " ".join(
                    [
                        result.name,
                        result.category,
                        result.description,
                        result.bin_id,
                        *result.aliases,
                    ]
                ).lower()
            ]
            return matches[clean_offset : clean_offset + clean_limit], len(matches)

        location_rows = self.client.select(
            "locations",
            columns="id,local_code",
            filters=self._filters(),
        )
        all_locations_by_uuid = {str(row["id"]): row for row in location_rows}
        locations_by_uuid = {
            location_id: row
            for location_id, row in all_locations_by_uuid.items()
            if str(row.get("local_code") or "") in wanted
        }
        if not locations_by_uuid:
            return [], 0
        location_filter = ",".join(locations_by_uuid)
        entries, total = self.client.select_page(
            "inventory_entries",
            filters=self._filters(location_id=f"in.({location_filter})"),
            order="location_id.asc,item_id.asc",
            limit=clean_limit,
            offset=clean_offset,
        )
        if not entries:
            return [], total

        item_ids = sorted({str(entry["item_id"]) for entry in entries})
        entry_ids = sorted({str(entry["id"]) for entry in entries})
        item_filter = ",".join(item_ids)
        entry_filter = ",".join(entry_ids)
        items = self.client.select(
            "items",
            filters=self._filters(id=f"in.({item_filter})", active="eq.true"),
        )
        aliases = self.client.select(
            "item_aliases",
            filters={"item_id": f"in.({item_filter})"},
        )
        review_links = self.client.select(
            "inventory_review_photos",
            filters=self._filters(inventory_entry_id=f"in.({entry_filter})"),
        )
        review_photo_ids = sorted(
            {str(link["photo_id"]) for link in review_links if link.get("photo_id")}
        )
        photo_or = f"item_id.in.({item_filter})"
        if review_photo_ids:
            photo_or += f",id.in.({','.join(review_photo_ids)})"
        photos = self.client.select(
            "photos",
            filters=self._filters(**{"or": f"({photo_or})"}),
        )

        items_by_uuid = {str(row["id"]): row for row in items}
        aliases_by_item: dict[str, list[str]] = {}
        for row in aliases:
            aliases_by_item.setdefault(str(row["item_id"]), []).append(
                str(row["alias"])
            )
        photos_by_item: dict[str, list[dict[str, object]]] = {}
        photos_by_uuid = {str(row["id"]): row for row in photos}
        for row in photos:
            if row.get("item_id"):
                photos_by_item.setdefault(str(row["item_id"]), []).append(row)
        review_by_entry: dict[str, list[dict[str, object]]] = {}
        for link in review_links:
            photo = photos_by_uuid.get(str(link.get("photo_id")))
            if photo is not None:
                review_by_entry.setdefault(
                    str(link["inventory_entry_id"]), []
                ).append(photo)

        results: list[SearchResult] = []
        for entry in entries:
            item = items_by_uuid.get(str(entry.get("item_id")))
            location = locations_by_uuid.get(str(entry.get("location_id")))
            if item is None or location is None:
                continue
            item_photos = [
                *photos_by_item.get(str(item["id"]), []),
                *review_by_entry.get(str(entry["id"]), []),
            ]
            results.append(
                SearchResult(
                    item_id=int(item["local_id"]),
                    name=str(item["name"]),
                    description=str(item.get("description") or ""),
                    category=str(item.get("category") or ""),
                    aliases=sorted(set(aliases_by_item.get(str(item["id"]), []))),
                    bin_id=str(location["local_code"]),
                    quantity=float(entry.get("quantity") or 0),
                    unit=str(entry.get("unit") or "each"),
                    needs_review=bool(entry.get("needs_review")),
                    review_reason=str(entry.get("review_reason") or ""),
                    photos=list(
                        dict.fromkeys(self._photo_url(photo) for photo in item_photos)
                    ),
                    size_class=str(item.get("size_class") or ""),
                    size_source=str(item.get("size_source") or ""),
                    size_confidence=float(item.get("size_confidence") or 0),
                    dimensions=(
                        dict(item["dimensions"])
                        if isinstance(item.get("dimensions"), dict)
                        else None
                    ),
                    home_location_id=self._location_code(
                        item.get("home_location_id"), all_locations_by_uuid
                    ),
                    last_seen_location_id=self._location_code(
                        item.get("last_seen_location_id"), all_locations_by_uuid
                    ),
                )
            )
        return results, total

    def _matching_item_uuids(self, query: str) -> set[str]:
        pattern = self._postgrest_ilike_pattern(query)
        items = self.client.select(
            "items",
            columns="id",
            filters=self._filters(
                active="eq.true",
                **{
                    "or": (
                        f"(name.ilike.{pattern},category.ilike.{pattern},"
                        f"description.ilike.{pattern})"
                    )
                }
            ),
            limit=5000,
        )
        aliases = self.client.select(
            "item_aliases",
            columns="item_id,items!inner(workspace_id)",
            filters={
                "alias": f"ilike.{pattern}",
                "items.workspace_id": f"eq.{self.workspace_id}",
                "items.active": "eq.true",
            },
            limit=5000,
        )
        return {
            str(row[item_key])
            for rows, item_key in ((items, "id"), (aliases, "item_id"))
            for row in rows
            if row.get(item_key)
        }

    @staticmethod
    def _location_content_columns() -> str:
        return (
            "id,item_id,location_id,quantity,unit,needs_review,review_reason,"
            "items!inner(local_id,name,description,category,size_class,size_source,"
            "size_confidence,dimensions,active,archived_at,"
            "home:locations!items_home_location_id_fkey(local_code),"
            "last_seen:locations!items_last_seen_location_id_fkey(local_code),"
            "item_aliases(alias),photos!photos_item_id_fkey(local_id)),"
            "locations!inner(local_code),"
            "inventory_review_photos("
            "photo:photos!inventory_review_photos_photo_id_fkey(local_id))"
        )

    def _embedded_location_content_results(
        self, entries: list[dict[str, object]]
    ) -> list[SearchResult]:
        results: list[SearchResult] = []
        for entry in entries:
            item = entry.get("items")
            location = entry.get("locations")
            if not isinstance(item, dict) or not isinstance(location, dict):
                continue
            aliases = item.get("item_aliases")
            item_photos = item.get("photos")
            review_links = entry.get("inventory_review_photos")
            photo_ids: list[int] = []
            if isinstance(item_photos, list):
                photo_ids.extend(
                    int(photo["local_id"])
                    for photo in item_photos
                    if isinstance(photo, dict) and photo.get("local_id") is not None
                )
            if isinstance(review_links, list):
                for link in review_links:
                    photo = link.get("photo") if isinstance(link, dict) else None
                    if isinstance(photo, dict) and photo.get("local_id") is not None:
                        photo_ids.append(int(photo["local_id"]))
            home = item.get("home")
            last_seen = item.get("last_seen")
            results.append(
                SearchResult(
                    item_id=int(item["local_id"]),
                    name=str(item["name"]),
                    description=str(item.get("description") or ""),
                    category=str(item.get("category") or ""),
                    aliases=sorted(
                        {
                            str(alias["alias"])
                            for alias in aliases or []
                            if isinstance(alias, dict) and alias.get("alias")
                        }
                    ),
                    bin_id=str(location.get("local_code") or ""),
                    quantity=float(entry.get("quantity") or 0),
                    unit=str(entry.get("unit") or "each"),
                    needs_review=bool(entry.get("needs_review")),
                    review_reason=str(entry.get("review_reason") or ""),
                    photos=[f"/photos/{photo_id}" for photo_id in dict.fromkeys(photo_ids)],
                    size_class=str(item.get("size_class") or ""),
                    size_source=str(item.get("size_source") or ""),
                    size_confidence=float(item.get("size_confidence") or 0),
                    dimensions=(
                        dict(item["dimensions"])
                        if isinstance(item.get("dimensions"), dict)
                        and item.get("dimensions")
                        else None
                    ),
                    home_location_id=(
                        str(home.get("local_code") or "")
                        if isinstance(home, dict)
                        else ""
                    ),
                    last_seen_location_id=(
                        str(last_seen.get("local_code") or "")
                        if isinstance(last_seen, dict)
                        else ""
                    ),
                    active=bool(item.get("active", True)),
                    archived_at=(
                        str(item["archived_at"]) if item.get("archived_at") else None
                    ),
                )
            )
        return results

    def location_item_counts(self) -> dict[str, int]:
        if isinstance(self.client, SupabaseRestClient):
            with self._inventory_cache_lock:
                if (
                    self._location_count_cache is not None
                    and monotonic() - self._location_count_cache_time
                    < INVENTORY_CACHE_TTL_SECONDS
                ):
                    return dict(self._location_count_cache)
                counts = {
                    location_id: int(metrics["item_count"])
                    for location_id, metrics in self.location_inventory_metrics().items()
                    if int(metrics["item_count"])
                }
                self._location_count_cache = counts
                self._location_count_cache_time = monotonic()
                return dict(counts)
        counts: dict[str, int] = {}
        for result in self._inventory_results():
            if not result.active:
                continue
            counts[result.bin_id] = counts.get(result.bin_id, 0) + 1
        return counts

    def location_inventory_metrics(self) -> dict[str, dict[str, object]]:
        if isinstance(self.client, SupabaseRestClient):
            payload = self.client.rpc(
                "magpie_location_inventory_metrics",
                {"target_workspace_id": self.workspace_id},
            )
            if not isinstance(payload, list):
                raise RuntimeError(
                    "Supabase location inventory metrics returned an invalid response"
                )
            metrics: dict[str, dict[str, object]] = {}
            for row in payload:
                if not isinstance(row, dict) or not row.get("location_code"):
                    continue
                metrics[str(row["location_code"])] = {
                    "item_count": int(row.get("item_count") or 0),
                    "review_count": int(row.get("review_count") or 0),
                    "content_revision": int(row.get("content_revision") or 0),
                }
            return metrics
        metrics: dict[str, dict[str, object]] = {}
        for result in self._inventory_results():
            if not result.active:
                continue
            row = metrics.setdefault(
                result.bin_id,
                {"item_count": 0, "review_count": 0, "content_revision": 0},
            )
            row["item_count"] = int(row["item_count"]) + 1
            if result.needs_review:
                row["review_count"] = int(row["review_count"]) + 1
        return metrics

    def location_review_counts(self) -> dict[str, int]:
        if isinstance(self.client, SupabaseRestClient):
            return {
                location_id: int(metrics["review_count"])
                for location_id, metrics in self.location_inventory_metrics().items()
                if int(metrics["review_count"])
            }
        counts: dict[str, int] = {}
        for result in self._inventory_results():
            if not result.active or not result.needs_review:
                continue
            counts[result.bin_id] = counts.get(result.bin_id, 0) + 1
        return counts

    def item_events(self, item_id: int, *, limit: int = 25) -> list[dict[str, object]]:
        rows = self.client.select(
            "repository_events",
            columns="local_id,event_type,item_local_id,location_code,quantity,note,created_at",
            filters=self._filters(item_local_id=f"eq.{int(item_id)}"),
            order="local_id.desc",
            limit=max(1, min(int(limit), 100)),
        )
        return [
            {
                "id": int(row["local_id"]),
                "event_type": str(row["event_type"]),
                "item_id": int(row["item_local_id"]),
                "bin_id": str(row.get("location_code") or ""),
                "quantity": (
                    float(row["quantity"]) if row.get("quantity") is not None else None
                ),
                "note": str(row.get("note") or ""),
                "created_at": row.get("created_at"),
            }
            for row in rows
        ]

    def suggest_home(self, name: str) -> str | None:
        needle = name.strip().lower()
        candidates = [
            result
            for result in self._inventory_results()
            if result.name.lower() == needle or result.name.lower() in needle
        ]
        if not candidates:
            return None
        preferred = sorted(
            candidates,
            key=lambda result: (not bool(result.home_location_id), result.bin_id),
        )[0]
        return preferred.home_location_id or preferred.bin_id

    def update_item_size(
        self,
        *,
        item_id: int,
        size_class: str,
        source: str = "user",
        confidence: float = 1.0,
        dimensions: dict[str, object] | None = None,
    ) -> None:
        clean_size = normalize_size_class(size_class)
        if not clean_size:
            raise ValueError("valid size class is required")
        item = self._require_item(item_id)
        self.client.update(
            "items",
            {
                "size_class": clean_size,
                "size_source": source.strip() or "user",
                "size_confidence": clamp_confidence(confidence),
                "dimensions": dimensions or {},
            },
            filters=self._filters(local_id=f"eq.{int(item_id)}"),
            return_rows=False,
        )
        self._record_event("size_learned", item=item, note=clean_size)

    def restore_item_size_metadata(
        self,
        *,
        item_id: int,
        size_class: str,
        source: str,
        confidence: float,
        dimensions: dict[str, object] | None,
    ) -> None:
        clean_size = normalize_size_class(size_class)
        if size_class and not clean_size:
            raise ValueError("valid size class is required")
        item = self._require_item(item_id)
        self.client.update(
            "items",
            {
                "size_class": clean_size,
                "size_source": source.strip(),
                "size_confidence": clamp_confidence(confidence),
                "dimensions": dimensions or {},
            },
            filters=self._filters(local_id=f"eq.{int(item_id)}"),
            return_rows=False,
        )
        self._record_event("size_restored", item=item, note=clean_size)

    def update_item_home(self, *, item_id: int, location_id: str) -> None:
        item = self._require_item(item_id)
        location = self._require_location(location_id)
        self.client.update(
            "items",
            {"home_location_id": str(location["id"])},
            filters=self._filters(local_id=f"eq.{int(item_id)}"),
            return_rows=False,
        )
        self._record_event("home_updated", item=item, location=location)

    def restore_item_location_metadata(
        self,
        *,
        item_id: int,
        home_location_id: str,
        last_seen_location_id: str,
        active: bool,
        archived_at: str | None,
    ) -> None:
        item = self._require_item(item_id)
        home = (
            self._require_location(home_location_id.strip())
            if home_location_id.strip()
            else None
        )
        last_seen = (
            self._require_location(last_seen_location_id.strip())
            if last_seen_location_id.strip()
            else None
        )
        self.client.update(
            "items",
            {
                "home_location_id": str(home["id"]) if home else None,
                "last_seen_location_id": str(last_seen["id"]) if last_seen else None,
                "active": bool(active),
                "archived_at": (
                    None
                    if active
                    else archived_at or datetime.now(UTC).isoformat()
                ),
            },
            filters=self._filters(local_id=f"eq.{int(item_id)}"),
            return_rows=False,
        )
        self._record_event(
            "item_location_metadata_restored",
            item=item,
            location=last_seen,
            note=json.dumps(
                {
                    "active": bool(active),
                    "home_location_id": home_location_id.strip(),
                },
                sort_keys=True,
            ),
        )
        self._invalidate_inventory_cache()

    def observe_confirmed_home(
        self,
        *,
        item_id: int,
        location_id: str,
        required_matches: int = 2,
        window: int = 3,
    ) -> dict[str, object]:
        required_matches = max(2, int(required_matches))
        window = max(required_matches, min(int(window), 10))
        item = self._require_item(item_id)
        location = self._require_location(location_id)
        events = self.client.select(
            "repository_events",
            columns="local_id,event_type,location_code,note",
            filters=self._filters(item_local_id=f"eq.{int(item_id)}"),
            order="local_id.desc",
            limit=100,
        )
        explicit_home = any(row.get("event_type") == "home_updated" for row in events)
        undone_ids = {
            int(row["note"])
            for row in events
            if row.get("event_type") == "home_observation_undone"
            and str(row.get("note") or "").isdigit()
        }
        inserted = self.client.insert(
            "repository_events",
            {
                "workspace_id": self.workspace_id,
                "event_type": "home_observed",
                "item_id": str(item["id"]),
                "item_local_id": int(item["local_id"]),
                "location_id": str(location["id"]),
                "location_code": str(location["local_code"]),
                "note": "confirmed",
            },
        )
        if not inserted:
            raise RuntimeError("Supabase home observation write returned no row")
        observation_id = int(inserted[0]["local_id"])
        active = [
            {"local_id": observation_id, "location_code": str(location["local_code"])}
        ]
        active.extend(
            row
            for row in events
            if row.get("event_type") == "home_observed"
            and int(row.get("local_id") or 0) not in undone_ids
        )
        observations = active[:window]
        clean_location = str(location["local_code"])
        matches = sum(
            1 for observation in observations
            if str(observation.get("location_code") or "") == clean_location
        )
        location_rows = self.client.select(
            "locations",
            columns="id,local_code",
            filters=self._filters(),
        )
        previous_home = self._location_code(
            item.get("home_location_id"),
            {str(row["id"]): row for row in location_rows},
        )
        promoted = (
            not explicit_home
            and clean_location != previous_home
            and matches >= required_matches
        )
        if promoted:
            self.client.update(
                "items",
                {"home_location_id": str(location["id"])},
                filters=self._filters(local_id=f"eq.{int(item_id)}"),
                return_rows=False,
            )
            self._record_event(
                "home_learned",
                item=item,
                location=location,
                note=f"{matches} of {len(observations)} confirmed",
            )
        return {
            "home_observation": observation_id,
            "item_id": int(item_id),
            "item": str(item.get("name") or ""),
            "location_id": clean_location,
            "matches": matches,
            "window": len(observations),
            "required_matches": required_matches,
            "explicit_home": explicit_home,
            "promoted": promoted,
            "previous_home_location_id": previous_home,
            "home_location_id": clean_location if promoted else previous_home,
        }

    def undo_confirmed_home_observation(self, observation_id: int) -> bool:
        rows = self.client.select(
            "repository_events",
            columns="local_id,item_local_id,location_code",
            filters=self._filters(local_id=f"eq.{int(observation_id)}", event_type="eq.home_observed"),
            limit=1,
        )
        if not rows:
            return False
        observation = rows[0]
        existing = self.client.select(
            "repository_events",
            columns="local_id",
            filters=self._filters(
                item_local_id=f"eq.{int(observation['item_local_id'])}",
                event_type="eq.home_observation_undone",
                note=f"eq.{int(observation_id)}",
            ),
            limit=1,
        )
        if existing:
            return False
        item = self._require_item(int(observation["item_local_id"]))
        location = self._require_location(str(observation.get("location_code") or ""))
        self._record_event(
            "home_observation_undone",
            item=item,
            location=location,
            note=str(int(observation_id)),
        )
        return True

    def clear_item_size(self, *, item_id: int) -> None:
        item = self._require_item(item_id)
        self.client.update(
            "items",
            {
                "size_class": "",
                "size_source": "",
                "size_confidence": 0,
                "dimensions": {},
            },
            filters=self._filters(local_id=f"eq.{int(item_id)}"),
            return_rows=False,
        )
        self._record_event("size_cleared", item=item)

    def item_size_class(self, name: str) -> str | None:
        needle = name.strip().lower()
        if not needle:
            return None
        items = self.client.select("items", filters=self._filters())
        aliases = self.client.select("item_aliases")
        aliases_by_item: dict[str, set[str]] = {}
        for row in aliases:
            aliases_by_item.setdefault(str(row["item_id"]), set()).add(
                str(row["alias"]).lower()
            )
        for item in items:
            if not item.get("size_class"):
                continue
            if str(item["name"]).lower() == needle or needle in aliases_by_item.get(
                str(item["id"]), set()
            ):
                return str(item["size_class"])
        return None

    def learn_rule(
        self,
        *,
        rule_text: str,
        trigger_terms: list[str],
        target_location_id: str,
        source_command: str = "",
        confidence: float = 1.0,
    ) -> int:
        clean_rule = rule_text.strip()
        if not clean_rule:
            raise ValueError("rule text is required")
        terms = normalize_rule_terms(trigger_terms or [clean_rule])
        if not terms:
            raise ValueError("at least one trigger term is required")
        location = self._require_location(target_location_id.strip())
        existing = next(
            (
                row
                for row in self.client.select(
                    "placement_rules", filters=self._filters(active="eq.true")
                )
                if str(row.get("rule_text") or "").lower() == clean_rule.lower()
            ),
            None,
        )
        values = {
            "rule_text": clean_rule,
            "trigger_terms": terms,
            "target_location_id": str(location["id"]),
            "source_command": source_command.strip(),
            "confidence": clamp_confidence(confidence),
            "active": True,
        }
        if existing is None:
            rows = self.client.insert(
                "placement_rules",
                {"workspace_id": self.workspace_id, **values},
            )
        else:
            rows = self.client.update(
                "placement_rules",
                values,
                filters=self._filters(id=f"eq.{existing['id']}"),
            )
        if not rows:
            raise RuntimeError("Supabase placement rule write returned no row")
        self._record_event("rule_learned", location=location, note=clean_rule)
        return int(rows[0]["local_id"])

    def learn_size_rule(
        self,
        *,
        rule_text: str,
        trigger_terms: list[str],
        size_class: str,
        source_command: str = "",
        confidence: float = 1.0,
    ) -> int:
        clean_rule = rule_text.strip()
        clean_size = normalize_size_class(size_class)
        if not clean_rule:
            raise ValueError("rule text is required")
        if not clean_size:
            raise ValueError("valid size class is required")
        terms = normalize_rule_terms(trigger_terms or [clean_rule])
        if not terms:
            raise ValueError("at least one trigger term is required")
        existing = next(
            (
                row
                for row in self.client.select(
                    "size_rules", filters=self._filters(active="eq.true")
                )
                if str(row.get("rule_text") or "").lower() == clean_rule.lower()
            ),
            None,
        )
        values = {
            "rule_text": clean_rule,
            "trigger_terms": terms,
            "size_class": clean_size,
            "source_command": source_command.strip(),
            "confidence": clamp_confidence(confidence),
            "active": True,
        }
        if existing is None:
            rows = self.client.insert(
                "size_rules",
                {"workspace_id": self.workspace_id, **values},
            )
        else:
            rows = self.client.update(
                "size_rules",
                values,
                filters=self._filters(id=f"eq.{existing['id']}"),
            )
        if not rows:
            raise RuntimeError("Supabase size rule write returned no row")
        self._record_event("size_rule_learned", note=clean_rule)
        return int(rows[0]["local_id"])

    def size_rules(
        self,
        *,
        active_only: bool = True,
        limit: int = 50,
    ) -> list[LearnedSizeRule]:
        filters = self._filters(**({"active": "eq.true"} if active_only else {}))
        rows = self.client.select(
            "size_rules",
            filters=filters,
            order="updated_at.desc,local_id.desc",
            limit=limit,
        )
        return [self._size_rule_from_row(row) for row in rows]

    def matching_size_rules(
        self,
        text: str,
        *,
        limit: int = 8,
    ) -> list[LearnedSizeRule]:
        query_terms = set(normalize_rule_terms([text]))
        if not query_terms:
            return []
        scored = [
            (len(query_terms & set(rule.trigger_terms)), rule)
            for rule in self.size_rules(active_only=True, limit=100)
            if query_terms & set(rule.trigger_terms)
        ]
        scored.sort(key=lambda entry: (-entry[0], -entry[1].confidence, entry[1].id))
        return [rule for _, rule in scored[:limit]]

    def rules(
        self,
        *,
        active_only: bool = True,
        limit: int = 50,
    ) -> list[LearnedRule]:
        filters = self._filters(**({"active": "eq.true"} if active_only else {}))
        rows = self.client.select(
            "placement_rules",
            filters=filters,
            order="updated_at.desc,local_id.desc",
            limit=limit,
        )
        locations = {
            str(row["id"]): str(row["local_code"])
            for row in self.client.select("locations", filters=self._filters())
        }
        return [self._rule_from_row(row, locations) for row in rows]

    def rule(self, rule_id: int, *, active_only: bool = True) -> LearnedRule | None:
        return next(
            (
                rule
                for rule in self.rules(active_only=active_only, limit=1000)
                if rule.id == int(rule_id)
            ),
            None,
        )

    def size_rule(
        self,
        rule_id: int,
        *,
        active_only: bool = True,
    ) -> LearnedSizeRule | None:
        return next(
            (
                rule
                for rule in self.size_rules(active_only=active_only, limit=1000)
                if rule.id == int(rule_id)
            ),
            None,
        )

    def matching_rules(self, text: str, *, limit: int = 8) -> list[LearnedRule]:
        query_terms = set(normalize_rule_terms([text]))
        if not query_terms:
            return []
        scored = [
            (len(query_terms & set(rule.trigger_terms)), rule)
            for rule in self.rules(active_only=True, limit=100)
            if query_terms & set(rule.trigger_terms)
        ]
        scored.sort(key=lambda entry: (-entry[0], -entry[1].confidence, entry[1].id))
        return [rule for _, rule in scored[:limit]]

    def rule_conflicts(
        self,
        text: str = "",
        *,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        placement = (
            self.matching_rules(text, limit=100)
            if text.strip()
            else self.rules(active_only=True, limit=100)
        )
        sizes = (
            self.matching_size_rules(text, limit=100)
            if text.strip()
            else self.size_rules(active_only=True, limit=100)
        )
        conflicts = _placement_rule_conflicts(placement, limit=limit)
        if len(conflicts) < limit:
            conflicts.extend(
                _size_rule_conflicts(sizes, limit=limit - len(conflicts))
            )
        return conflicts

    def update_rule(
        self,
        *,
        rule_id: int,
        rule_text: str,
        trigger_terms: list[str],
        target_location_id: str,
        source_command: str = "",
        confidence: float = 1.0,
    ) -> bool:
        current = self.rule(rule_id)
        if current is None:
            return False
        clean_rule = rule_text.strip()
        terms = normalize_rule_terms(trigger_terms or [clean_rule])
        if not clean_rule:
            raise ValueError("rule text is required")
        if not terms:
            raise ValueError("at least one trigger term is required")
        location = self._require_location(target_location_id.strip())
        rows = self.client.update(
            "placement_rules",
            {
                "rule_text": clean_rule,
                "trigger_terms": terms,
                "target_location_id": str(location["id"]),
                "source_command": source_command.strip(),
                "confidence": clamp_confidence(confidence),
            },
            filters=self._filters(local_id=f"eq.{int(rule_id)}", active="eq.true"),
        )
        if rows:
            self._record_event(
                "rule_updated",
                location=location,
                note=f"{rule_id}:{current.rule_text} -> {clean_rule}",
            )
        return bool(rows)

    def update_size_rule(
        self,
        *,
        rule_id: int,
        rule_text: str,
        trigger_terms: list[str],
        size_class: str,
        source_command: str = "",
        confidence: float = 1.0,
    ) -> bool:
        current = self.size_rule(rule_id)
        if current is None:
            return False
        clean_rule = rule_text.strip()
        clean_size = normalize_size_class(size_class)
        terms = normalize_rule_terms(trigger_terms or [clean_rule])
        if not clean_rule:
            raise ValueError("rule text is required")
        if not clean_size:
            raise ValueError("valid size class is required")
        if not terms:
            raise ValueError("at least one trigger term is required")
        rows = self.client.update(
            "size_rules",
            {
                "rule_text": clean_rule,
                "trigger_terms": terms,
                "size_class": clean_size,
                "source_command": source_command.strip(),
                "confidence": clamp_confidence(confidence),
            },
            filters=self._filters(local_id=f"eq.{int(rule_id)}", active="eq.true"),
        )
        if rows:
            self._record_event(
                "size_rule_updated",
                note=f"{rule_id}:{current.rule_text} -> {clean_rule}",
            )
        return bool(rows)

    def deactivate_rule(self, *, rule_id: int, rule_type: str) -> bool:
        table = {"placement": "placement_rules", "size": "size_rules"}.get(
            rule_type
        )
        if table is None:
            raise ValueError("rule_type must be placement or size")
        rows = self.client.update(
            table,
            {"active": False},
            filters=self._filters(local_id=f"eq.{int(rule_id)}", active="eq.true"),
        )
        if rows:
            self._record_event(
                "rule_forgotten",
                note=f"{rule_type}:{rule_id}:{rows[0].get('rule_text', '')}",
            )
        return bool(rows)

    def set_rule_active(
        self, *, rule_id: int, rule_type: str, active: bool
    ) -> bool:
        table = {"placement": "placement_rules", "size": "size_rules"}.get(
            rule_type
        )
        if table is None:
            raise ValueError("rule_type must be placement or size")
        rows = self.client.update(
            table,
            {"active": bool(active)},
            filters=self._filters(local_id=f"eq.{int(rule_id)}"),
        )
        if rows:
            self._record_event(
                "rule_restored" if active else "rule_forgotten",
                note=f"{rule_type}:{rule_id}:{rows[0].get('rule_text', '')}",
            )
        return bool(rows)

    def record_example(
        self,
        *,
        command: str,
        action: str,
        kind: str = "confirmed",
        context: dict[str, object] | None = None,
    ) -> int | None:
        clean = command.strip()
        if not clean:
            return None
        outcome = "corrected" if "correction" in kind else "confirmed"
        rows = self.client.insert(
            "agent_examples",
            {
                "workspace_id": self.workspace_id,
                "command": clean,
                "action": action,
                "context": context or {},
                "kind": kind,
                "outcome": outcome,
                "review_status": "pending",
            },
        )
        if not rows:
            raise RuntimeError("Supabase example write returned no row")
        return int(rows[0]["local_id"])

    def approve_example(self, example_id: int) -> bool:
        return self._set_example_review_status(
            example_id,
            status="approved",
            event_type="example_approved",
        )

    def reject_example(self, example_id: int) -> bool:
        return self._set_example_review_status(
            example_id,
            status="rejected",
            event_type="example_rejected",
        )

    def mark_example_promoted(self, example_id: int) -> bool:
        return self._set_example_review_status(
            example_id,
            status="promoted",
            event_type="example_promoted",
        )

    def example(self, example_id: int) -> dict[str, object] | None:
        rows = self.client.select(
            "agent_examples",
            filters=self._filters(local_id=f"eq.{int(example_id)}"),
            limit=1,
        )
        return self._example_from_row(rows[0]) if rows else None

    def record_assistant_interaction(
        self,
        *,
        route: str,
        command: str,
        photo_ids: list[int] | None = None,
        response: dict[str, object] | None = None,
        status: str = "",
    ) -> int:
        rows = self.client.insert(
            "assistant_interactions",
            {
                "workspace_id": self.workspace_id,
                "route": route.strip(),
                "command": command.strip(),
                "photo_local_ids": [int(photo_id) for photo_id in photo_ids or []],
                "status": status.strip(),
                "response": response or {},
            },
        )
        if not rows:
            raise RuntimeError("Supabase assistant interaction insert returned no row")
        return int(rows[0]["local_id"])

    def load_active_guidance_state(self) -> dict[str, object]:
        rows = self.client.select(
            "active_guidance_states",
            filters=self._filters(),
            limit=1,
        )
        if not rows:
            return {"entries": [], "next_id": 1}
        state = rows[0].get("state")
        return state if isinstance(state, dict) else {"entries": [], "next_id": 1}

    def save_active_guidance_state(self, state: dict[str, object]) -> None:
        rows = self.client.upsert(
            "active_guidance_states",
            {
                "workspace_id": self.workspace_id,
                "state": state,
            },
            on_conflict="workspace_id",
        )
        if not rows:
            raise RuntimeError("Supabase active guidance upsert returned no row")

    def assistant_interactions_after(
        self,
        after_id: int = 0,
        *,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        clean_limit = max(1, min(int(limit), 100))
        filters = self._filters()
        if after_id > 0:
            filters["local_id"] = f"gt.{int(after_id)}"
        rows = self.client.select(
            "assistant_interactions",
            filters=filters,
            order=f"local_id.{('asc' if after_id > 0 else 'desc')}",
            limit=clean_limit,
        )
        if after_id <= 0:
            rows.reverse()
        return [self._interaction_from_row(row) for row in rows]

    def assistant_interaction(self, interaction_id: int) -> dict[str, object] | None:
        rows = self.client.select(
            "assistant_interactions",
            filters=self._filters(local_id=f"eq.{int(interaction_id)}"),
            limit=1,
        )
        return self._interaction_from_row(rows[0]) if rows else None

    def assistant_interactions_by_ids(
        self, interaction_ids: list[int]
    ) -> list[dict[str, object]]:
        wanted = sorted({int(value) for value in interaction_ids if int(value) > 0})[:100]
        if not wanted:
            return []
        rows = self.client.select(
            "assistant_interactions",
            filters=self._filters(
                local_id=self._postgrest_in([str(value) for value in wanted])
            ),
            order="local_id.asc",
            limit=len(wanted),
        )
        return [self._interaction_from_row(row) for row in rows]

    def assistant_interaction_by_client_request_id(
        self,
        client_request_id: str,
        *,
        route: str = "",
    ) -> dict[str, object] | None:
        clean_id = client_request_id.strip()
        if not clean_id:
            return None
        filters = self._filters()
        filters["response->>client_request_id"] = f"eq.{clean_id}"
        if route:
            filters["route"] = f"eq.{route}"
        rows = self.client.select(
            "assistant_interactions",
            filters=filters,
            order="local_id.desc",
            limit=1,
        )
        return self._interaction_from_row(rows[0]) if rows else None

    def latest_pending_agent_plan(self) -> dict[str, object] | None:
        rows = self.client.select(
            "assistant_interactions",
            filters=self._filters(
                status="eq.pending",
                route=self._postgrest_in(
                    [
                        "/assistant/agent",
                        "/assistant/chat",
                        "/assistant/revise",
                        "/mcp/propose_plan",
                    ]
                ),
            ),
            order="local_id.desc",
            limit=1,
        )
        if not rows:
            return None
        latest = self._interaction_from_row(rows[0])
        plan = latest["response"].get("plan")
        if not isinstance(plan, list) or not plan:
            return None
        return latest

    def supersede_pending_agent_plans(self) -> int:
        """Close older proposals before recording a replacement plan."""
        rows = self.client.update(
            "assistant_interactions",
            {"status": "superseded"},
            filters=self._filters(
                status="eq.pending",
                route=self._postgrest_in(
                    [
                        "/assistant/agent",
                        "/assistant/chat",
                        "/assistant/revise",
                        "/mcp/propose_plan",
                    ]
                ),
            ),
        )
        return len(rows)

    def latest_undoable_confirmed_plan(self) -> dict[str, object] | None:
        undoable_routes = {
            "/assistant/confirm",
            "/assistant/voice/confirm",
            "/locations/create",
            "/locations/create-batch",
            "/locations/create-structure",
            "/locations/update",
            "/locations/move",
            "/locations/archive",
            "/locations/restore",
            "/inventory/home",
            "/inventory/manual",
            "/rules/update",
            "/rules/disable",
            "/rules/restore",
            "/storage/setup",
        }
        rows = self.client.select(
            "assistant_interactions",
            filters=self._filters(
                status="neq.undone",
                route=self._postgrest_in(sorted(undoable_routes)),
            ),
            order="local_id.desc",
            limit=25,
        )
        for row in rows:
            interaction = self._interaction_from_row(row)
            if interaction["route"] not in undoable_routes:
                continue
            if isinstance(interaction["response"].get("applied"), list) and interaction[
                "response"
            ]["applied"]:
                return interaction
        return None

    def set_assistant_interaction_status(
        self,
        interaction_id: int,
        status: str,
    ) -> bool:
        rows = self.client.update(
            "assistant_interactions",
            {"status": status.strip()},
            filters=self._filters(local_id=f"eq.{int(interaction_id)}"),
        )
        return bool(rows)

    def claim_assistant_interaction_confirmation(self, interaction_id: int) -> bool:
        """Atomically reserve a pending proposal for one confirmation request."""
        rows = self.client.update(
            "assistant_interactions",
            {"status": "confirming"},
            filters=self._filters(
                local_id=f"eq.{int(interaction_id)}",
                status="eq.pending",
            ),
        )
        return bool(rows)

    def record_voice_attempt(
        self,
        *,
        rms: float | None = None,
        activity_rms: float | None = None,
        transcript: str = "",
        wake_word: str = "",
        wake_heard: bool = False,
        awaiting_command: bool = False,
        command: str = "",
        route: str = "",
        query: str = "",
        stt_model: str = "",
        audio_threshold: float = 0.0,
        device_id: str = "",
        runtime_version: str = "",
        backend_response: dict[str, object] | None = None,
        error: str = "",
    ) -> int:
        rows = self.client.insert(
            "voice_attempts",
            {
                "workspace_id": self.workspace_id,
                "rms": rms,
                "activity_rms": activity_rms,
                "transcript": transcript.strip(),
                "wake_word": wake_word.strip(),
                "wake_heard": bool(wake_heard),
                "awaiting_command": bool(awaiting_command),
                "command": command.strip(),
                "route": route.strip(),
                "query": query.strip(),
                "stt_model": stt_model.strip(),
                "audio_threshold": float(audio_threshold),
                "device_id": device_id.strip(),
                "runtime_version": runtime_version.strip(),
                "backend_response": backend_response or {},
                "error": error.strip(),
            },
        )
        if not rows:
            raise RuntimeError("Supabase voice attempt insert returned no row")
        return int(rows[0]["local_id"])

    def update_assistant_interaction_response(
        self,
        interaction_id: int,
        patch: dict[str, object],
    ) -> bool:
        interaction = self.assistant_interaction(interaction_id)
        if interaction is None:
            return False
        response = dict(interaction["response"])
        response.update(patch)
        rows = self.client.update(
            "assistant_interactions",
            {"response": response},
            filters=self._filters(local_id=f"eq.{int(interaction_id)}"),
        )
        return bool(rows)

    def recent_voice_attempts(self, *, limit: int = 20) -> list[dict[str, object]]:
        clean_limit = max(1, min(int(limit), 100))
        rows = self.client.select(
            "voice_attempts",
            filters=self._filters(),
            order="local_id.desc",
            limit=clean_limit,
        )
        return [self._voice_attempt_from_row(row) for row in reversed(rows)]

    def recent_voice_command_attempts(
        self,
        *,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        clean_limit = max(1, min(int(limit), 100))
        rows = self.client.select(
            "voice_attempts",
            filters={
                **self._filters(),
                "or": "(wake_heard.eq.true,command.neq.,query.neq.)",
            },
            order="local_id.desc",
            limit=100,
        )
        attempts = [
            self._voice_attempt_from_row(row)
            for row in rows
            if str(row.get("error") or "") != "awaiting_continuation"
            and (
                bool(str(row.get("command") or "").strip())
                or bool(str(row.get("query") or "").strip())
                or (
                    bool(row.get("wake_heard"))
                    and not bool(row.get("awaiting_command"))
                )
            )
        ]
        return list(reversed(attempts[:clean_limit]))

    def recent_examples(self, *, limit: int = 5) -> list[dict[str, object]]:
        rows = self.client.select("agent_examples", filters=self._filters())
        rows = [row for row in rows if row.get("review_status") != "rejected"]
        rows.sort(key=lambda row: int(row["local_id"]), reverse=True)
        return [self._example_from_row(row) for row in rows[:limit]]

    def find_examples(self, query: str, *, limit: int = 3) -> list[dict[str, object]]:
        terms = [term for term in query.lower().split() if len(term) > 2]
        if not terms:
            return self.recent_examples(limit=limit)
        priority = {
            "correction": 4,
            "review_correction": 4,
            "edited_photo_sort": 3,
            "review_approved": 3,
            "rejected_photo_sort": 2,
        }
        rows = self.client.select("agent_examples", filters=self._filters())
        rows = [
            row
            for row in rows
            if row.get("review_status") != "rejected"
            and any(term in str(row.get("command") or "").lower() for term in terms)
        ]
        rows.sort(
            key=lambda row: (
                priority.get(str(row.get("kind") or ""), 1),
                int(row["local_id"]),
            ),
            reverse=True,
        )
        return [self._example_from_row(row) for row in rows[:limit]]

    def examples_for_eval(
        self,
        *,
        limit: int = 100,
        kind: str | None = None,
        review_status: str | None = None,
        include_rejected: bool = False,
    ) -> list[dict[str, object]]:
        clean_limit = max(1, min(int(limit), 500))
        extra_filters: dict[str, str] = {}
        if kind:
            extra_filters["kind"] = f"eq.{kind}"
        if review_status:
            extra_filters["review_status"] = f"eq.{review_status}"
        elif not include_rejected:
            extra_filters["review_status"] = "neq.rejected"
        rows = self.client.select(
            "agent_examples",
            filters=self._filters(**extra_filters),
            order="local_id.desc",
            limit=clean_limit,
        )
        return [self._example_from_row(row) for row in rows]

    def example_review_counts(self) -> dict[str, int]:
        counts = {"pending": 0, "approved": 0, "promoted": 0, "rejected": 0}
        for row in self.client.select("agent_examples", filters=self._filters()):
            status = str(row.get("review_status") or "pending")
            counts[status] = counts.get(status, 0) + 1
        counts["active"] = counts["pending"] + counts["approved"] + counts["promoted"]
        counts["total"] = counts["active"] + counts["rejected"]
        return counts

    def example_kind_counts(self, *, include_rejected: bool = False) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.client.select("agent_examples", filters=self._filters()):
            if not include_rejected and row.get("review_status") == "rejected":
                continue
            kind = str(row.get("kind") or "example")
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    def add_photo(
        self,
        *,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> PhotoRecord:
        clean_filename = Path(filename).name or "photo"
        clean_content_type = content_type.strip() or "application/octet-stream"
        suffix = Path(clean_filename).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}:
            suffix = suffix_for_content_type(clean_content_type)
        storage_path = f"{self.workspace_id}/{uuid4().hex}{suffix}"
        self.client.upload_object(
            bucket=self.photo_bucket,
            path=storage_path,
            data=data,
            content_type=clean_content_type,
        )
        try:
            rows = self.client.insert(
                "photos",
                {
                    "workspace_id": self.workspace_id,
                    "object_type": None,
                    "object_id": None,
                    "storage_path": storage_path,
                    "filename": clean_filename,
                    "content_type": clean_content_type,
                },
            )
        except Exception:
            self.client.delete_objects(bucket=self.photo_bucket, paths=[storage_path])
            raise
        if not rows:
            self.client.delete_objects(bucket=self.photo_bucket, paths=[storage_path])
            raise RuntimeError("Supabase photo insert returned no row")
        return self._photo_from_row(rows[0])

    def attach_photo(self, *, item_id: int, photo_id: int) -> None:
        item = self._require_item(item_id)
        rows = self.client.update(
            "photos",
            {
                "item_id": str(item["id"]),
                "object_type": "item",
                "object_id": str(item["id"]),
            },
            filters=self._filters(local_id=f"eq.{int(photo_id)}"),
        )
        if not rows:
            raise ValueError(f"photo {photo_id} not found")
        self._invalidate_inventory_cache()

    def attach_photo_to_location(self, *, location_id: str, photo_id: int) -> None:
        location = self._require_location(location_id)
        rows = self.client.update(
            "photos",
            {
                "location_id": str(location["id"]),
                "object_type": "location",
                "object_id": str(location["id"]),
            },
            filters=self._filters(local_id=f"eq.{int(photo_id)}"),
        )
        if not rows:
            raise ValueError(f"photo {photo_id} not found")
        self._invalidate_inventory_cache()

    def set_photo_target(
        self,
        *,
        photo_id: int,
        item_id: int | None = None,
        location_id: str | None = None,
    ) -> None:
        item = self._item(item_id) if item_id is not None else None
        location = self._location(location_id) if location_id is not None else None
        self.client.update(
            "photos",
            {
                "item_id": str(item["id"]) if item is not None else None,
                "location_id": str(location["id"]) if location is not None else None,
                "object_type": (
                    "item" if item is not None else "location" if location is not None else None
                ),
                "object_id": (
                    str(item["id"])
                    if item is not None
                    else str(location["id"])
                    if location is not None
                    else None
                ),
            },
            filters=self._filters(local_id=f"eq.{int(photo_id)}"),
            return_rows=False,
        )
        self._invalidate_inventory_cache()

    def location_photos(self, location_id: str) -> list[PhotoRecord]:
        location = self._location(location_id)
        if location is None:
            return []
        rows = self.client.select(
            "photos",
            filters=self._filters(location_id=f"eq.{location['id']}"),
            order="local_id.asc",
        )
        return [self._photo_from_row(row) for row in rows]

    def location_photos_by_location(
        self, location_ids: list[str]
    ) -> dict[str, list[PhotoRecord]]:
        wanted = {str(location_id) for location_id in location_ids if str(location_id)}
        if not wanted:
            return {}
        rows = self.client.select(
            "photos",
            columns="*,items(local_id),locations!inner(local_code)",
            filters=self._filters(),
            order="local_id.asc",
        )
        grouped: dict[str, list[PhotoRecord]] = {}
        for row in rows:
            location = row.get("locations")
            location_id = (
                str(location.get("local_code") or "")
                if isinstance(location, dict)
                else ""
            )
            if location_id in wanted:
                grouped.setdefault(location_id, []).append(self._photo_from_row(row))
        return grouped

    def delete_photo(self, photo_id: int) -> None:
        photo = self._photo(photo_id)
        if photo is None:
            return
        self.client.delete(
            "photos",
            filters=self._filters(local_id=f"eq.{int(photo_id)}"),
            return_rows=False,
        )
        self.client.delete_objects(
            bucket=self.photo_bucket,
            paths=[str(photo["storage_path"])],
        )
        self._invalidate_inventory_cache()

    def photo(self, photo_id: int) -> PhotoRecord | None:
        row = self._photo(photo_id)
        return self._photo_from_row(row) if row is not None else None

    def photo_content(self, photo_id: int) -> bytes:
        row = self._photo(photo_id)
        if row is None:
            raise FileNotFoundError(photo_id)
        return self.client.download_object(
            bucket=self.photo_bucket,
            path=str(row["storage_path"]),
        )

    def _inventory_results(self) -> list[SearchResult]:
        with self._inventory_cache_lock:
            if (
                self._inventory_cache is not None
                and monotonic() - self._inventory_cache_time
                < INVENTORY_CACHE_TTL_SECONDS
            ):
                return list(self._inventory_cache)
            results = self._load_inventory_results()
            self._inventory_cache = results
            self._inventory_cache_time = monotonic()
            return list(results)

    def _load_inventory_results(self) -> list[SearchResult]:
        try:
            entries = self.client.select(
                "inventory_entries",
                columns=self._location_content_columns(),
                filters=self._filters(),
                order="item_id.asc,location_id.asc",
            )
            results = self._embedded_location_content_results(entries)
            if len(results) == len(entries):
                return results
        except (KeyError, TypeError, ValueError, SupabaseError):
            # Older projects may not have refreshed PostgREST relationship
            # metadata yet. Keep the compatibility assembler until they do.
            pass

        items = self.client.select("items", filters=self._filters())
        locations = self.client.select("locations", filters=self._filters())
        entries = self.client.select("inventory_entries", filters=self._filters())
        photos = self.client.select("photos", filters=self._filters())
        review_photos = self.client.select(
            "inventory_review_photos", filters=self._filters()
        )
        aliases = self.client.select("item_aliases")
        items_by_uuid = {str(row["id"]): row for row in items}
        locations_by_uuid = {str(row["id"]): row for row in locations}
        aliases_by_item: dict[str, list[str]] = {}
        for row in aliases:
            aliases_by_item.setdefault(str(row["item_id"]), []).append(str(row["alias"]))
        photos_by_item: dict[str, list[dict[str, object]]] = {}
        photos_by_uuid = {str(row["id"]): row for row in photos}
        for row in photos:
            if row.get("item_id"):
                photos_by_item.setdefault(str(row["item_id"]), []).append(row)
        review_by_entry: dict[str, list[dict[str, object]]] = {}
        for link in review_photos:
            photo = photos_by_uuid.get(str(link.get("photo_id")))
            if photo is not None:
                review_by_entry.setdefault(str(link["inventory_entry_id"]), []).append(
                    photo
                )
        results: list[SearchResult] = []
        for entry in entries:
            item = items_by_uuid.get(str(entry.get("item_id")))
            location = locations_by_uuid.get(str(entry.get("location_id")))
            if item is None or location is None:
                continue
            item_photos = photos_by_item.get(str(item["id"]), [])
            item_photos += review_by_entry.get(str(entry["id"]), [])
            results.append(
                SearchResult(
                    item_id=int(item["local_id"]),
                    name=str(item["name"]),
                    description=str(item.get("description") or ""),
                    category=str(item.get("category") or ""),
                    aliases=sorted(set(aliases_by_item.get(str(item["id"]), []))),
                    bin_id=str(location["local_code"]),
                    quantity=float(entry.get("quantity") or 0),
                    unit=str(entry.get("unit") or "each"),
                    needs_review=bool(entry.get("needs_review")),
                    review_reason=str(entry.get("review_reason") or ""),
                    photos=list(
                        dict.fromkeys(
                            self._photo_url(photo) for photo in item_photos
                        )
                    ),
                    size_class=str(item.get("size_class") or ""),
                    size_source=str(item.get("size_source") or ""),
                    size_confidence=float(item.get("size_confidence") or 0),
                    dimensions=(
                        dict(item["dimensions"])
                        if isinstance(item.get("dimensions"), dict)
                        else None
                    ),
                    home_location_id=self._location_code(
                        item.get("home_location_id"), locations_by_uuid
                    ),
                    last_seen_location_id=self._location_code(
                        item.get("last_seen_location_id"), locations_by_uuid
                    ),
                    active=bool(item.get("active", True)),
                    archived_at=(
                        str(item["archived_at"]) if item.get("archived_at") else None
                    ),
                )
            )
        return results

    def _invalidate_inventory_cache(self) -> None:
        with self._inventory_cache_lock:
            self._inventory_cache = None
            self._inventory_cache_time = 0.0
            self._location_count_cache = None
            self._location_count_cache_time = 0.0

    def _set_example_review_status(
        self,
        example_id: int,
        *,
        status: str,
        event_type: str,
    ) -> bool:
        example = self.example(example_id)
        if example is None:
            return False
        rows = self.client.update(
            "agent_examples",
            {"review_status": status, "reviewed_at": datetime.now(UTC).isoformat()},
            filters=self._filters(local_id=f"eq.{int(example_id)}"),
        )
        if rows:
            self._record_event(
                event_type,
                note=f"{example_id}:{example['kind']}:{example['command']}",
            )
        return bool(rows)

    @staticmethod
    def _example_from_row(row: dict[str, object]) -> dict[str, object]:
        return {
            "id": int(row["local_id"]),
            "command": str(row.get("command") or ""),
            "action": str(row.get("action") or ""),
            "context_json": json.dumps(row.get("context") or {}, sort_keys=True),
            "kind": str(row.get("kind") or "confirmed"),
            "review_status": str(row.get("review_status") or "pending"),
            "reviewed_at": row.get("reviewed_at"),
            "created_at": row.get("created_at"),
        }

    @staticmethod
    def _interaction_from_row(row: dict[str, object]) -> dict[str, object]:
        response = row.get("response")
        photo_ids = row.get("photo_local_ids")
        return {
            "id": int(row["local_id"]),
            "route": str(row.get("route") or ""),
            "command": str(row.get("command") or ""),
            "photo_ids": (
                [int(photo_id) for photo_id in photo_ids]
                if isinstance(photo_ids, list)
                else []
            ),
            "status": str(row.get("status") or ""),
            "response": dict(response) if isinstance(response, dict) else {},
            "created_at": row.get("created_at"),
        }

    @staticmethod
    def _voice_attempt_from_row(row: dict[str, object]) -> dict[str, object]:
        backend_response = row.get("backend_response")
        return {
            "id": int(row["local_id"]),
            "rms": row.get("rms"),
            "activity_rms": row.get("activity_rms"),
            "transcript": str(row.get("transcript") or ""),
            "wake_word": str(row.get("wake_word") or ""),
            "wake_heard": bool(row.get("wake_heard")),
            "awaiting_command": bool(row.get("awaiting_command")),
            "command": str(row.get("command") or ""),
            "route": str(row.get("route") or ""),
            "query": str(row.get("query") or ""),
            "stt_model": str(row.get("stt_model") or ""),
            "audio_threshold": float(row.get("audio_threshold") or 0),
            "device_id": str(row.get("device_id") or ""),
            "runtime_version": str(row.get("runtime_version") or ""),
            "backend_response": (
                dict(backend_response) if isinstance(backend_response, dict) else {}
            ),
            "error": str(row.get("error") or ""),
            "created_at": row.get("created_at"),
        }

    @staticmethod
    def _rule_from_row(
        row: dict[str, object],
        locations: dict[str, str],
    ) -> LearnedRule:
        terms = row.get("trigger_terms")
        return LearnedRule(
            id=int(row["local_id"]),
            rule_text=str(row["rule_text"]),
            trigger_terms=[str(term) for term in terms] if isinstance(terms, list) else [],
            target_location_id=locations.get(str(row["target_location_id"]), ""),
            source_command=str(row.get("source_command") or ""),
            confidence=float(row.get("confidence") or 0),
            active=bool(row.get("active")),
        )

    @staticmethod
    def _size_rule_from_row(row: dict[str, object]) -> LearnedSizeRule:
        terms = row.get("trigger_terms")
        return LearnedSizeRule(
            id=int(row["local_id"]),
            rule_text=str(row["rule_text"]),
            trigger_terms=[str(term) for term in terms] if isinstance(terms, list) else [],
            size_class=str(row["size_class"]),
            source_command=str(row.get("source_command") or ""),
            confidence=float(row.get("confidence") or 0),
            active=bool(row.get("active")),
        )

    def _item(self, item_id: int) -> dict[str, object] | None:
        rows = self.client.select(
            "items",
            filters=self._filters(local_id=f"eq.{int(item_id)}"),
            limit=1,
        )
        return rows[0] if rows else None

    def _require_item(self, item_id: int) -> dict[str, object]:
        item = self._item(item_id)
        if item is None:
            raise ValueError("item not found")
        return item

    def _location(self, location_id: str) -> dict[str, object] | None:
        rows = self.client.select(
            "locations",
            filters=self._filters(local_code=f"eq.{location_id}"),
            limit=1,
        )
        return rows[0] if rows else None

    def _require_location(self, location_id: str) -> dict[str, object]:
        location = self._location(location_id)
        if location is None:
            raise ValueError(f"location not found: {location_id}")
        return location

    def _entry(
        self,
        item_id: int,
        location_id: str,
    ) -> tuple[
        dict[str, object] | None,
        dict[str, object] | None,
        dict[str, object] | None,
    ]:
        item = self._item(item_id)
        location = self._location(location_id)
        if item is None or location is None:
            return None, item, location
        rows = self.client.select(
            "inventory_entries",
            filters=self._filters(
                item_id=f"eq.{item['id']}",
                location_id=f"eq.{location['id']}",
            ),
            limit=1,
        )
        return (rows[0] if rows else None), item, location

    def _photo(self, photo_id: int) -> dict[str, object] | None:
        rows = self.client.select(
            "photos",
            filters=self._filters(local_id=f"eq.{int(photo_id)}"),
            limit=1,
        )
        return rows[0] if rows else None

    def _record_event(
        self,
        event_type: str,
        *,
        item: dict[str, object] | None = None,
        location: dict[str, object] | None = None,
        quantity: float | None = None,
        note: str = "",
    ) -> None:
        self._invalidate_inventory_cache()
        self.client.insert(
            "repository_events",
            {
                "workspace_id": self.workspace_id,
                "event_type": event_type,
                "item_id": str(item["id"]) if item is not None else None,
                "item_local_id": int(item["local_id"]) if item is not None else None,
                "location_id": str(location["id"]) if location is not None else None,
                "location_code": (
                    str(location["local_code"]) if location is not None else ""
                ),
                "quantity": quantity,
                "note": note,
            },
            return_rows=False,
        )

    def _filters(self, **filters: str) -> dict[str, str]:
        return {"workspace_id": f"eq.{self.workspace_id}", **filters}

    @staticmethod
    def _postgrest_in(values: list[str]) -> str:
        quoted = []
        for value in values:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            quoted.append(f'"{escaped}"')
        return f"in.({','.join(quoted)})"

    @staticmethod
    def _postgrest_ilike_pattern(value: str) -> str:
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("*", "\\*")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        return f'"*{escaped}*"'

    def _photo_url(self, photo: dict[str, object]) -> str:
        return f"/photos/{int(photo['local_id'])}"

    def _photo_from_row(self, row: dict[str, object]) -> PhotoRecord:
        item = None
        location = None
        embedded_item = row.get("items")
        if isinstance(embedded_item, dict) and embedded_item.get("local_id") is not None:
            item = int(embedded_item["local_id"])
        elif "items" not in row and row.get("item_id"):
            items = self.client.select(
                "items",
                filters=self._filters(id=f"eq.{row['item_id']}"),
                limit=1,
            )
            item = int(items[0]["local_id"]) if items else None
        embedded_location = row.get("locations")
        if isinstance(embedded_location, dict) and embedded_location.get("local_code"):
            location = str(embedded_location["local_code"])
        elif "locations" not in row and row.get("location_id"):
            locations = self.client.select(
                "locations",
                filters=self._filters(id=f"eq.{row['location_id']}"),
                limit=1,
            )
            location = str(locations[0]["local_code"]) if locations else None
        storage_path = str(row["storage_path"])
        return PhotoRecord(
            id=int(row["local_id"]),
            filename=str(row.get("filename") or Path(storage_path).name),
            content_type=str(row.get("content_type") or "application/octet-stream"),
            path=storage_path,
            url=f"/photos/{int(row['local_id'])}",
            item_id=item,
            location_id=location,
        )

    @staticmethod
    def _location_code(
        location_uuid: object,
        locations_by_uuid: dict[str, dict[str, object]],
    ) -> str:
        if not location_uuid:
            return ""
        row = locations_by_uuid.get(str(location_uuid))
        return str(row["local_code"]) if row is not None else ""
