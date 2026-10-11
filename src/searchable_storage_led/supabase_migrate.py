from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from .config import LedConfig
from .locations import LocationStore
from .store import InventoryStore, suffix_for_content_type
from .supabase_check import check_supabase
from .supabase_client import SupabaseRestClient


SOURCE_TABLES = (
    "locations",
    "items",
    "aliases",
    "inventory",
    "photos",
    "inventory_review_photos",
    "hardware_bindings",
    "placement_rules",
    "size_rules",
    "examples",
    "assistant_interactions",
    "voice_attempts",
    "events",
)


class SqliteToSupabaseMigrator:
    def __init__(
        self,
        *,
        source_path: str | Path,
        client: Any,
        workspace_id: str,
        photo_bucket: str = "magpie-photos",
        batch_size: int = 100,
    ) -> None:
        self.source_path = Path(source_path)
        self.client = client
        self.workspace_id = workspace_id.strip()
        self.photo_bucket = photo_bucket.strip()
        self.batch_size = int(batch_size)
        if not self.workspace_id:
            raise ValueError("Supabase workspace_id is required")
        if not self.photo_bucket:
            raise ValueError("Supabase photo bucket is required")
        if self.batch_size <= 0:
            raise ValueError("migration batch_size must be positive")

    def run(self, *, dry_run: bool = False) -> dict[str, object]:
        if not self.source_path.exists():
            raise FileNotFoundError(self.source_path)
        if not dry_run:
            # Bring older POC databases up to the latest schema before reading.
            InventoryStore(self.source_path)
            LocationStore(self.source_path)
        with self._connect(read_only=dry_run) as conn:
            counts = {table: self._count(conn, table) for table in SOURCE_TABLES}
            referenced_locations = self._referenced_location_codes(conn)
            source_locations = [
                dict(row) for row in self._rows(conn, "locations")
            ]
            existing_locations = {str(row["id"]) for row in source_locations}
            placeholder_codes = sorted(referenced_locations - existing_locations)
            location_conflicts = self._active_location_name_conflicts(
                source_locations,
                placeholder_codes,
            )
            if dry_run:
                return {
                    "schema": "magpie.supabase_migration.v1",
                    "dry_run": True,
                    "ready": not location_conflicts,
                    "source": str(self.source_path),
                    "workspace_id": self.workspace_id,
                    "batch_size": self.batch_size,
                    "source_counts": counts,
                    "placeholder_locations": placeholder_codes,
                    "location_name_conflicts": location_conflicts,
                    "warnings": self._photo_warnings(conn),
                }

            if location_conflicts:
                raise ValueError(
                    "source contains ambiguous active sibling location names: "
                    + json.dumps(location_conflicts, sort_keys=True)
                )
            if self.client is None:
                raise ValueError("Supabase client is required for migration")

            check_supabase(self.client, workspace_id=self.workspace_id)
            migrated: dict[str, int] = {}
            warnings: list[str] = []
            locations = self._migrate_locations(
                source_locations,
                placeholder_codes,
            )
            migrated["locations"] = len(locations)
            items = self._migrate_items(conn, locations)
            migrated["items"] = len(items)
            migrated["item_aliases"] = self._migrate_aliases(conn, items)
            entries = self._migrate_inventory(conn, items, locations)
            migrated["inventory_entries"] = len(entries)
            photos, photo_warnings = self._migrate_photos(conn, items, locations)
            warnings.extend(photo_warnings)
            migrated["photos"] = len(photos)
            migrated["inventory_review_photos"] = self._migrate_review_photos(
                conn,
                entries,
                photos,
            )
            migrated["hardware_bindings"] = self._migrate_hardware(
                conn,
                locations,
            )
            migrated["placement_rules"] = self._migrate_placement_rules(
                conn,
                locations,
            )
            migrated["size_rules"] = self._migrate_size_rules(conn)
            migrated["agent_examples"] = self._migrate_examples(conn)
            migrated["assistant_interactions"] = self._migrate_interactions(conn)
            migrated["voice_attempts"] = self._migrate_voice_attempts(conn)
            migrated["repository_events"] = self._migrate_events(
                conn,
                items,
                locations,
            )
            self.client.rpc("advance_magpie_identity_sequences")
            target_counts = self.client.rpc(
                "magpie_workspace_counts",
                {"target_workspace_id": self.workspace_id},
            )
            self._verify_counts(migrated, target_counts)
        return {
            "schema": "magpie.supabase_migration.v1",
            "dry_run": False,
            "ready": not warnings,
            "source": str(self.source_path),
            "workspace_id": self.workspace_id,
            "batch_size": self.batch_size,
            "source_counts": counts,
            "migrated": migrated,
            "target_counts": target_counts,
            "placeholder_locations": placeholder_codes,
            "location_name_conflicts": [],
            "warnings": warnings,
        }

    def _migrate_locations(
        self,
        source_rows: list[dict[str, object]],
        placeholders: list[str],
    ) -> dict[str, dict[str, object]]:
        source = list(source_rows)
        source.extend(
            {
                "id": code,
                "parent_id": None,
                "type": "bin",
                "label": code,
                "grid_json": None,
                "cell_col": None,
                "cell_row": None,
                "cell_w": 1,
                "cell_h": 1,
                "light_target": None,
                "light_indexes": None,
                "capacity_class": "medium",
                "purpose_tags": None,
                "placement_notes": "Migrated placeholder for referenced location",
                "active": 1,
                "archived_at": None,
            }
            for code in placeholders
        )
        remote: dict[str, dict[str, object]] = {}
        pending = {str(row["id"]): row for row in source}
        while pending:
            ready = [
                row
                for row in pending.values()
                if not row.get("parent_id")
                or str(row["parent_id"]) in remote
            ]
            if not ready:
                unresolved = {
                    code: str(row.get("parent_id") or "")
                    for code, row in sorted(pending.items())
                }
                raise ValueError(
                    "source contains cyclic or missing location parents: "
                    + json.dumps(unresolved, sort_keys=True)
                )
            for row in ready:
                code = str(row["id"])
                parent_code = str(row.get("parent_id") or "")
                parent = remote.get(parent_code)
                values = self._location_values(
                    row,
                    parent_id=str(parent["id"]) if parent else None,
                )
                written = self.client.upsert(
                    "locations",
                    values,
                    on_conflict="workspace_id,local_code",
                )
                if written:
                    remote[code] = written[0]
                pending.pop(code)
        return remote

    def _location_values(
        self,
        row: dict[str, object],
        *,
        parent_id: str | None,
    ) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "local_code": str(row["id"]),
            "parent_id": parent_id,
            "type": str(row.get("type") or "bin"),
            "label": str(row.get("label") or row["id"]),
            "grid": self._json(row.get("grid_json"), {}),
            "cell_col": row.get("cell_col"),
            "cell_row": row.get("cell_row"),
            "cell_w": int(row.get("cell_w") or 1),
            "cell_h": int(row.get("cell_h") or 1),
            "light_target": row.get("light_target"),
            "light_indexes": self._int_list(row.get("light_indexes")),
            "capacity_class": str(row.get("capacity_class") or "medium"),
            "purpose_tags": self._string_list(row.get("purpose_tags")),
            "placement_notes": str(row.get("placement_notes") or ""),
            "internal_dimensions": self._json(
                row.get("internal_dimensions_json"), {}
            ),
            "active": bool(row.get("active", 1)),
            "archived_at": row.get("archived_at"),
            "archive_group": (
                None
                if bool(row.get("active", 1))
                else str(row.get("archive_group") or row["id"])
            ),
        }

    def _migrate_items(
        self,
        conn: sqlite3.Connection,
        locations: dict[str, dict[str, object]],
    ) -> dict[int, dict[str, object]]:
        remote: dict[int, dict[str, object]] = {}
        for row in self._rows(conn, "items"):
            home = locations.get(str(row["home_location_id"] or ""))
            last_seen = locations.get(str(row["last_seen_location_id"] or ""))
            written = self.client.upsert(
                "items",
                {
                    "workspace_id": self.workspace_id,
                    "local_id": int(row["id"]),
                    "name": str(row["name"]),
                    "description": str(row["description"] or ""),
                    "category": str(row["category"] or ""),
                    "size_class": str(row["size_class"] or ""),
                    "size_source": str(row["size_source"] or ""),
                    "size_confidence": float(row["size_confidence"] or 0),
                    "dimensions": self._json(row["dimensions_json"], {}),
                    "home_location_id": str(home["id"]) if home else None,
                    "last_seen_location_id": str(last_seen["id"]) if last_seen else None,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                },
                on_conflict="workspace_id,local_id",
            )
            if written:
                remote[int(row["id"])] = written[0]
        return remote

    def _migrate_aliases(
        self,
        conn: sqlite3.Connection,
        items: dict[int, dict[str, object]],
    ) -> int:
        migrated = 0
        for row in self._rows(conn, "aliases"):
            item = items.get(int(row["item_id"]))
            if item is None:
                continue
            self.client.upsert(
                "item_aliases",
                {"item_id": str(item["id"]), "alias": str(row["alias"])},
                on_conflict="item_id,alias",
                return_rows=False,
            )
            migrated += 1
        return migrated

    def _migrate_inventory(
        self,
        conn: sqlite3.Connection,
        items: dict[int, dict[str, object]],
        locations: dict[str, dict[str, object]],
    ) -> dict[tuple[int, str], dict[str, object]]:
        remote: dict[tuple[int, str], dict[str, object]] = {}
        for row in self._rows(conn, "inventory"):
            item_id = int(row["item_id"])
            location_code = str(row["bin_id"])
            item = items.get(item_id)
            location = locations.get(location_code)
            if item is None or location is None:
                continue
            needs_review = bool(row["needs_review"])
            written = self.client.upsert(
                "inventory_entries",
                {
                    "workspace_id": self.workspace_id,
                    "item_id": str(item["id"]),
                    "location_id": str(location["id"]),
                    "quantity": float(row["quantity"]),
                    "unit": str(row["unit"] or "each"),
                    "status": "review" if needs_review else "present",
                    "source": "import",
                    "needs_review": needs_review,
                    "review_reason": str(row["review_reason"] or ""),
                    "updated_at": row["updated_at"],
                },
                on_conflict="workspace_id,item_id,location_id",
            )
            if written:
                remote[(item_id, location_code)] = written[0]
        return remote

    def _migrate_photos(
        self,
        conn: sqlite3.Connection,
        items: dict[int, dict[str, object]],
        locations: dict[str, dict[str, object]],
    ) -> tuple[dict[int, dict[str, object]], list[str]]:
        remote: dict[int, dict[str, object]] = {}
        warnings: list[str] = []
        for row in self._rows(conn, "photos"):
            photo_id = int(row["id"])
            source = self._photo_source_path(row["path"])
            if not source.exists():
                warnings.append(f"photo {photo_id} missing source file: {source}")
                continue
            content_type = str(row["content_type"] or "application/octet-stream")
            suffix = source.suffix.lower() or suffix_for_content_type(content_type)
            storage_path = f"{self.workspace_id}/imported-{photo_id}{suffix}"
            self.client.upload_object(
                bucket=self.photo_bucket,
                path=storage_path,
                data=source.read_bytes(),
                content_type=content_type,
                upsert=True,
            )
            item = items.get(int(row["item_id"])) if row["item_id"] is not None else None
            location = locations.get(str(row["location_id"] or ""))
            target = item or location
            written = self.client.upsert(
                "photos",
                {
                    "workspace_id": self.workspace_id,
                    "local_id": photo_id,
                    "object_type": "item" if item else "location" if location else None,
                    "object_id": str(target["id"]) if target else None,
                    "storage_path": storage_path,
                    "filename": str(row["filename"]),
                    "content_type": content_type,
                    "item_id": str(item["id"]) if item else None,
                    "location_id": str(location["id"]) if location else None,
                    "created_at": row["created_at"],
                },
                on_conflict="workspace_id,local_id",
            )
            if written:
                remote[photo_id] = written[0]
        return remote, warnings

    def _migrate_review_photos(
        self,
        conn: sqlite3.Connection,
        entries: dict[tuple[int, str], dict[str, object]],
        photos: dict[int, dict[str, object]],
    ) -> int:
        migrated = 0
        for row in self._rows(conn, "inventory_review_photos"):
            entry = entries.get((int(row["item_id"]), str(row["bin_id"])))
            photo = photos.get(int(row["photo_id"]))
            if entry is None or photo is None:
                continue
            self.client.upsert(
                "inventory_review_photos",
                {
                    "workspace_id": self.workspace_id,
                    "inventory_entry_id": str(entry["id"]),
                    "photo_id": str(photo["id"]),
                    "created_at": row["created_at"],
                },
                on_conflict="inventory_entry_id,photo_id",
                return_rows=False,
            )
            migrated += 1
        return migrated

    def _migrate_hardware(
        self,
        conn: sqlite3.Connection,
        locations: dict[str, dict[str, object]],
    ) -> int:
        migrated = 0
        for row in self._rows(conn, "hardware_bindings"):
            location = locations.get(str(row["location_id"]))
            if location is None:
                continue
            self.client.upsert(
                "hardware_bindings",
                {
                    "workspace_id": self.workspace_id,
                    "local_id": int(row["id"]),
                    "location_id": str(location["id"]),
                    "kind": str(row["kind"]),
                    "controller_id": str(row["controller_id"] or ""),
                    "address": str(row["address"] or ""),
                    "metadata": self._json(row["metadata_json"], {}),
                    "active": bool(row["active"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                },
                on_conflict="workspace_id,local_id",
                return_rows=False,
            )
            migrated += 1
        return migrated

    def _migrate_placement_rules(
        self,
        conn: sqlite3.Connection,
        locations: dict[str, dict[str, object]],
    ) -> int:
        migrated = 0
        for row in self._rows(conn, "placement_rules"):
            location = locations.get(str(row["target_location_id"]))
            if location is None:
                continue
            self.client.upsert(
                "placement_rules",
                {
                    "workspace_id": self.workspace_id,
                    "local_id": int(row["id"]),
                    "rule_text": str(row["rule_text"]),
                    "trigger_terms": self._csv_list(row["trigger_terms"]),
                    "target_location_id": str(location["id"]),
                    "source_command": str(row["source_command"] or ""),
                    "confidence": float(row["confidence"]),
                    "active": bool(row["active"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                },
                on_conflict="workspace_id,local_id",
                return_rows=False,
            )
            migrated += 1
        return migrated

    def _migrate_size_rules(self, conn: sqlite3.Connection) -> int:
        migrated = 0
        for row in self._rows(conn, "size_rules"):
            self.client.upsert(
                "size_rules",
                {
                    "workspace_id": self.workspace_id,
                    "local_id": int(row["id"]),
                    "rule_text": str(row["rule_text"]),
                    "trigger_terms": self._csv_list(row["trigger_terms"]),
                    "size_class": str(row["size_class"]),
                    "source_command": str(row["source_command"] or ""),
                    "confidence": float(row["confidence"]),
                    "active": bool(row["active"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                },
                on_conflict="workspace_id,local_id",
                return_rows=False,
            )
            migrated += 1
        return migrated

    def _migrate_examples(self, conn: sqlite3.Connection) -> int:
        migrated = 0
        for row in self._rows(conn, "examples"):
            action = str(row["action"] or "")
            parsed_action = self._json(action, [])
            kind = str(row["kind"] or "confirmed")
            outcome = (
                "rejected"
                if "rejected" in kind
                else "corrected"
                if "correction" in kind
                else "confirmed"
            )
            self.client.upsert(
                "agent_examples",
                {
                    "workspace_id": self.workspace_id,
                    "local_id": int(row["id"]),
                    "command": str(row["command"]),
                    "context": self._json(row["context_json"], {}),
                    "proposed_plan": [],
                    "confirmed_plan": parsed_action if isinstance(parsed_action, list) else [],
                    "action": action,
                    "kind": kind,
                    "outcome": outcome,
                    "review_status": str(row["review_status"] or "pending"),
                    "reviewed_at": row["reviewed_at"],
                    "created_at": row["created_at"],
                },
                on_conflict="workspace_id,local_id",
                return_rows=False,
            )
            migrated += 1
        return migrated

    def _migrate_interactions(self, conn: sqlite3.Connection) -> int:
        values: list[dict[str, object]] = []
        for row in self._rows(conn, "assistant_interactions"):
            values.append(
                {
                    "workspace_id": self.workspace_id,
                    "local_id": int(row["id"]),
                    "route": str(row["route"]),
                    "command": str(row["command"]),
                    "photo_local_ids": self._csv_int_list(row["photo_ids"]),
                    "status": str(row["status"] or ""),
                    "response": self._json(row["response_json"], {}),
                    "created_at": row["created_at"],
                }
            )
        self._upsert_in_batches(
            "assistant_interactions",
            values,
            on_conflict="workspace_id,local_id",
        )
        return len(values)

    def _migrate_voice_attempts(self, conn: sqlite3.Connection) -> int:
        values: list[dict[str, object]] = []
        for row in self._rows(conn, "voice_attempts"):
            values.append(
                {
                    "workspace_id": self.workspace_id,
                    "local_id": int(row["id"]),
                    "rms": row["rms"],
                    "activity_rms": row["activity_rms"],
                    "transcript": str(row["transcript"] or ""),
                    "wake_word": str(row["wake_word"] or ""),
                    "wake_heard": bool(row["wake_heard"]),
                    "awaiting_command": bool(row["awaiting_command"]),
                    "command": str(row["command"] or ""),
                    "route": str(row["route"] or ""),
                    "query": str(row["query"] or ""),
                    "stt_model": str(row["stt_model"] or ""),
                    "audio_threshold": float(row["audio_threshold"] or 0),
                    "device_id": str(row["device_id"] or ""),
                    "runtime_version": str(row["runtime_version"] or ""),
                    "backend_response": self._json(row["backend_response_json"], {}),
                    "error": str(row["error"] or ""),
                    "created_at": row["created_at"],
                }
            )
        self._upsert_in_batches(
            "voice_attempts",
            values,
            on_conflict="workspace_id,local_id",
        )
        return len(values)

    def _migrate_events(
        self,
        conn: sqlite3.Connection,
        items: dict[int, dict[str, object]],
        locations: dict[str, dict[str, object]],
    ) -> int:
        values: list[dict[str, object]] = []
        for row in self._rows(conn, "events"):
            local_item_id = int(row["item_id"]) if row["item_id"] is not None else None
            location_code = str(row["bin_id"] or "")
            item = items.get(local_item_id) if local_item_id is not None else None
            location = locations.get(location_code)
            values.append(
                {
                    "workspace_id": self.workspace_id,
                    "local_id": int(row["id"]),
                    "event_type": str(row["event_type"]),
                    "item_id": str(item["id"]) if item else None,
                    "item_local_id": local_item_id,
                    "location_id": str(location["id"]) if location else None,
                    "location_code": location_code,
                    "quantity": row["quantity"],
                    "note": str(row["note"] or ""),
                    "created_at": row["created_at"],
                }
            )
        self._upsert_in_batches(
            "repository_events",
            values,
            on_conflict="workspace_id,local_id",
        )
        return len(values)

    def _upsert_in_batches(
        self,
        table: str,
        values: list[dict[str, object]],
        *,
        on_conflict: str,
    ) -> None:
        for start in range(0, len(values), self.batch_size):
            self.client.upsert(
                table,
                values[start : start + self.batch_size],
                on_conflict=on_conflict,
                return_rows=False,
            )

    @staticmethod
    def _verify_counts(migrated: dict[str, int], target_counts: object) -> None:
        if not isinstance(target_counts, dict):
            raise RuntimeError("Supabase migration count audit returned invalid data")
        shortfalls = {
            table: {"expected_at_least": count, "actual": target_counts.get(table)}
            for table, count in migrated.items()
            if not isinstance(target_counts.get(table), int)
            or int(target_counts[table]) < count
        }
        if shortfalls:
            raise RuntimeError(
                "Supabase migration count audit failed: "
                + json.dumps(shortfalls, sort_keys=True)
            )

    def _referenced_location_codes(self, conn: sqlite3.Connection) -> set[str]:
        codes: set[str] = set()
        queries = (
            "SELECT bin_id AS code FROM inventory",
            "SELECT home_location_id AS code FROM items",
            "SELECT last_seen_location_id AS code FROM items",
            "SELECT location_id AS code FROM photos",
            "SELECT location_id AS code FROM hardware_bindings",
            "SELECT target_location_id AS code FROM placement_rules",
        )
        for query in queries:
            for row in conn.execute(query):
                code = str(row["code"] or "").strip()
                if code:
                    codes.add(code)
        return codes

    def _photo_warnings(self, conn: sqlite3.Connection) -> list[str]:
        return [
            f"photo {int(row['id'])} missing source file: "
            f"{self._photo_source_path(row['path'])}"
            for row in self._rows(conn, "photos")
            if not self._photo_source_path(row["path"]).exists()
        ]

    @classmethod
    def _active_location_name_conflicts(
        cls,
        source: list[dict[str, object]],
        placeholders: list[str],
    ) -> list[dict[str, object]]:
        groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
        rows = list(source)
        rows.extend(
            {
                "id": code,
                "parent_id": None,
                "label": code,
                "active": 1,
            }
            for code in placeholders
        )
        for row in rows:
            if not bool(row.get("active", 1)):
                continue
            label = str(row.get("label") or row.get("id") or "")
            normalized = cls._normalize_location_name(label)
            parent_id = str(row.get("parent_id") or "")
            groups.setdefault((parent_id, normalized), []).append(
                (str(row["id"]), label)
            )
        conflicts = []
        for (parent_id, _normalized), matches in sorted(groups.items()):
            if len(matches) < 2:
                continue
            conflicts.append(
                {
                    "parent_id": parent_id or None,
                    "label": matches[0][1],
                    "location_ids": sorted(code for code, _label in matches),
                }
            )
        return conflicts

    @staticmethod
    def _normalize_location_name(value: object) -> str:
        return re.sub(r"\s+", " ", str(value).strip()).lower()

    def _photo_source_path(self, stored_path: object) -> Path:
        path = Path(str(stored_path)).expanduser()
        if path.is_absolute():
            return path
        app_relative = self.source_path.resolve().parent.parent / path
        if app_relative.exists():
            return app_relative
        return path

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            conn = sqlite3.connect(f"file:{self.source_path}?mode=ro", uri=True)
        else:
            conn = sqlite3.connect(self.source_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
        return conn.execute(f'SELECT * FROM "{table}"').fetchall()

    @staticmethod
    def _count(conn: sqlite3.Connection, table: str) -> int:
        return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])

    def _filters(self, **filters: str) -> dict[str, str]:
        return {"workspace_id": f"eq.{self.workspace_id}", **filters}

    @staticmethod
    def _json(value: object, default: Any) -> Any:
        if value is None or value == "":
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(str(value))
        except json.JSONDecodeError:
            return default

    @classmethod
    def _int_list(cls, value: object) -> list[int]:
        parsed = cls._json(value, [])
        return [int(item) for item in parsed] if isinstance(parsed, list) else []

    @classmethod
    def _string_list(cls, value: object) -> list[str]:
        parsed = cls._json(value, [])
        return [str(item) for item in parsed] if isinstance(parsed, list) else []

    @staticmethod
    def _csv_list(value: object) -> list[str]:
        return [part.strip() for part in str(value or "").split(",") if part.strip()]

    @staticmethod
    def _csv_int_list(value: object) -> list[int]:
        return [
            int(part)
            for part in str(value or "").split(",")
            if part.strip().isdigit()
        ]


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="searchable-storage-migrate-supabase",
        description="Migrate a Magpie SQLite database and photos into Supabase.",
    )
    parser.add_argument("--config")
    parser.add_argument("--database")
    parser.add_argument(
        "--workspace-id",
        default="",
        help="Target workspace for the import; required when the config is request-scoped.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    config = LedConfig.from_path(args.config)
    storage = config.storage
    if storage.backend != "supabase" and not args.dry_run:
        raise SystemExit("storage.backend must be supabase")
    workspace_id = (args.workspace_id or storage.workspace_id).strip()
    if not args.dry_run and not workspace_id:
        raise SystemExit(
            "--workspace-id is required when storage.workspace_id is not configured"
        )
    client = None
    if not args.dry_run:
        api_key = os.environ.get(storage.api_key_env, "").strip()
        if not api_key:
            raise SystemExit(
                f"Supabase API key environment variable is not set: {storage.api_key_env}"
            )
        client = SupabaseRestClient(
            url=storage.supabase_url,
            api_key=api_key,
            timeout_seconds=storage.timeout_seconds,
        )
    summary = SqliteToSupabaseMigrator(
        source_path=resolve_source_path(
            config_path=args.config,
            configured_path=config.database_path,
            override=args.database,
        ),
        client=client,
        workspace_id=workspace_id or "not-configured",
        photo_bucket=storage.photo_bucket,
        batch_size=args.batch_size,
    ).run(dry_run=args.dry_run)
    print(json.dumps(summary, indent=2, sort_keys=True))


def resolve_source_path(
    *,
    config_path: str | None,
    configured_path: str,
    override: str | None = None,
) -> Path:
    source = Path(override or configured_path).expanduser()
    if source.is_absolute() or override or not config_path:
        return source
    # Installed configs live at <app>/config/led-controller.json while the
    # service resolves state paths from <app>.
    return Path(config_path).expanduser().resolve().parent.parent / source


if __name__ == "__main__":
    main()
