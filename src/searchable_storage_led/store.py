from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from .layout import normalize_inventory_location_id
from .item_identity import identities_conflict, inferred_dimensions

if TYPE_CHECKING:
    from .locations import Location


@dataclass(frozen=True)
class SearchResult:
    item_id: int
    name: str
    description: str
    category: str
    aliases: list[str]
    bin_id: str
    quantity: float
    unit: str
    needs_review: bool
    review_reason: str
    photos: list[str]
    size_class: str = ""
    size_source: str = ""
    size_confidence: float = 0.0
    dimensions: dict[str, object] | None = None
    home_location_id: str = ""
    last_seen_location_id: str = ""
    active: bool = True
    archived_at: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class InventoryAuditConflictError(ValueError):
    """Inventory changed after a physical contents check was opened."""


@dataclass(frozen=True)
class InventoryCatalogPage:
    items: list[SearchResult]
    total: int
    metrics: dict[str, int]
    categories: list[str]


@dataclass(frozen=True)
class PhotoRecord:
    id: int
    filename: str
    content_type: str
    path: str
    url: str
    item_id: int | None = None
    location_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class LearnedRule:
    id: int
    rule_text: str
    trigger_terms: list[str]
    target_location_id: str
    source_command: str
    confidence: float
    active: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class LearnedSizeRule:
    id: int
    rule_text: str
    trigger_terms: list[str]
    size_class: str
    source_command: str
    confidence: float
    active: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class InventoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.create_collation("MAGPIE_NATURAL", _natural_compare)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS aliases (
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    alias TEXT NOT NULL,
                    UNIQUE(item_id, alias)
                );

                CREATE TABLE IF NOT EXISTS inventory (
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    bin_id TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 1,
                    unit TEXT NOT NULL DEFAULT 'each',
                    needs_review INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(item_id, bin_id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    item_id INTEGER,
                    bin_id TEXT,
                    quantity REAL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS photos (
                    id INTEGER PRIMARY KEY,
                    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
                    location_id TEXT,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    path TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS inventory_review_photos (
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    bin_id TEXT NOT NULL,
                    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(item_id, bin_id, photo_id)
                );

                -- The compounding loop: confirmed commands and corrections become
                -- reusable few-shot examples and a growing regression/eval set.
                CREATE TABLE IF NOT EXISTS examples (
                    id INTEGER PRIMARY KEY,
                    command TEXT NOT NULL,
                    action TEXT NOT NULL,
                    context_json TEXT NOT NULL DEFAULT '{}',
                    kind TEXT NOT NULL DEFAULT 'confirmed',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS assistant_interactions (
                    id INTEGER PRIMARY KEY,
                    route TEXT NOT NULL,
                    command TEXT NOT NULL,
                    photo_ids TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    response_json TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS assistant_interactions_client_request_idx
                ON assistant_interactions(
                    CASE WHEN json_valid(response_json)
                        THEN json_extract(response_json, '$.client_request_id')
                    END,
                    route,
                    id DESC
                );

                CREATE TABLE IF NOT EXISTS active_guidance_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    state_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS voice_attempts (
                    id INTEGER PRIMARY KEY,
                    rms REAL,
                    activity_rms REAL,
                    transcript TEXT NOT NULL DEFAULT '',
                    wake_word TEXT NOT NULL DEFAULT '',
                    wake_heard INTEGER NOT NULL DEFAULT 0,
                    awaiting_command INTEGER NOT NULL DEFAULT 0,
                    command TEXT NOT NULL DEFAULT '',
                    route TEXT NOT NULL DEFAULT '',
                    query TEXT NOT NULL DEFAULT '',
                    stt_model TEXT NOT NULL DEFAULT '',
                    audio_threshold REAL NOT NULL DEFAULT 0,
                    device_id TEXT NOT NULL DEFAULT '',
                    runtime_version TEXT NOT NULL DEFAULT '',
                    backend_response_json TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS placement_rules (
                    id INTEGER PRIMARY KEY,
                    rule_text TEXT NOT NULL,
                    trigger_terms TEXT NOT NULL,
                    target_location_id TEXT NOT NULL,
                    source_command TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS size_rules (
                    id INTEGER PRIMARY KEY,
                    rule_text TEXT NOT NULL,
                    trigger_terms TEXT NOT NULL,
                    size_class TEXT NOT NULL,
                    source_command TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            ensure_column(conn, "items", "size_class", "TEXT NOT NULL DEFAULT ''")
            ensure_column(conn, "items", "size_source", "TEXT NOT NULL DEFAULT ''")
            ensure_column(conn, "items", "size_confidence", "REAL NOT NULL DEFAULT 0")
            ensure_column(conn, "items", "dimensions_json", "TEXT")
            ensure_column(conn, "items", "home_location_id", "TEXT NOT NULL DEFAULT ''")
            ensure_column(conn, "items", "last_seen_location_id", "TEXT NOT NULL DEFAULT ''")
            ensure_column(conn, "items", "active", "INTEGER NOT NULL DEFAULT 1")
            ensure_column(conn, "items", "archived_at", "TEXT")
            ensure_column(conn, "items", "merged_into_item_id", "INTEGER")
            ensure_column(conn, "inventory", "needs_review", "INTEGER NOT NULL DEFAULT 0")
            ensure_column(conn, "inventory", "review_reason", "TEXT NOT NULL DEFAULT ''")
            ensure_column(conn, "photos", "location_id", "TEXT")
            ensure_column(conn, "assistant_interactions", "status", "TEXT NOT NULL DEFAULT ''")
            ensure_column(conn, "examples", "review_status", "TEXT NOT NULL DEFAULT 'pending'")
            ensure_column(conn, "examples", "reviewed_at", "TEXT")
            ensure_column(conn, "examples", "context_json", "TEXT NOT NULL DEFAULT '{}'")
            ensure_column(conn, "voice_attempts", "activity_rms", "REAL")
            ensure_column(conn, "voice_attempts", "device_id", "TEXT NOT NULL DEFAULT ''")
            ensure_column(conn, "voice_attempts", "runtime_version", "TEXT NOT NULL DEFAULT ''")
            self._backfill_inferred_dimensions(conn)

        with self.connect() as conn:
            has_locations = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'locations'"
            ).fetchone()
        if has_locations is not None:
            from .locations import LocationStore

            LocationStore(self.path)

    @staticmethod
    def _backfill_inferred_dimensions(conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT id, name FROM items WHERE dimensions_json IS NULL OR dimensions_json = ''"
        ).fetchall()
        for row in rows:
            dimensions = inferred_dimensions(str(row["name"]))
            if dimensions:
                conn.execute(
                    "UPDATE items SET dimensions_json = ? WHERE id = ?",
                    (json.dumps(dimensions, sort_keys=True), int(row["id"])),
                )

    def upsert_item(
        self,
        *,
        name: str,
        description: str = "",
        category: str = "",
        aliases: list[str] | None = None,
        photo_ids: list[int] | None = None,
    ) -> int:
        aliases = aliases or []
        photo_ids = photo_ids or []
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("item name is required")
        dimensions = inferred_dimensions(clean_name)
        with self.connect() as conn:
            merged = conn.execute(
                "SELECT merged_into_item_id FROM items WHERE name = ?",
                (clean_name,),
            ).fetchone()
            if merged is not None and merged["merged_into_item_id"] is not None:
                item_id = int(merged["merged_into_item_id"])
                for alias in [clean_name, *aliases]:
                    clean_alias = alias.strip()
                    if clean_alias:
                        conn.execute(
                            "INSERT OR IGNORE INTO aliases(item_id, alias) VALUES (?, ?)",
                            (item_id, clean_alias),
                        )
                for photo_id in photo_ids:
                    conn.execute(
                        "UPDATE photos SET item_id = ? WHERE id = ?",
                        (item_id, int(photo_id)),
                    )
                conn.execute(
                    "INSERT INTO events(event_type, item_id, note) VALUES ('item_upsert', ?, ?)",
                    (item_id, clean_name),
                )
                return item_id
            conn.execute(
                """
                INSERT INTO items(name, description, category, dimensions_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    description = excluded.description,
                    category = excluded.category,
                    active = 1,
                    archived_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    clean_name,
                    description.strip(),
                    category.strip(),
                    json.dumps(dimensions, sort_keys=True) if dimensions else None,
                ),
            )
            item_id = int(
                conn.execute("SELECT id FROM items WHERE name = ?", (clean_name,)).fetchone()[
                    "id"
                ]
            )
            for alias in aliases:
                clean_alias = alias.strip()
                if clean_alias:
                    conn.execute(
                        "INSERT OR IGNORE INTO aliases(item_id, alias) VALUES (?, ?)",
                        (item_id, clean_alias),
                    )
            for photo_id in photo_ids:
                conn.execute(
                    "UPDATE photos SET item_id = ? WHERE id = ?",
                    (item_id, int(photo_id)),
                )
            conn.execute(
                "INSERT INTO events(event_type, item_id, note) VALUES ('item_upsert', ?, ?)",
                (item_id, clean_name),
            )
        return item_id

    def archive_item(self, item_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT name FROM items WHERE id = ? AND active = 1",
                (int(item_id),),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                """
                UPDATE items
                SET active = 0,
                    archived_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(item_id),),
            )
            conn.execute(
                "INSERT INTO events(event_type, item_id, note) "
                "VALUES ('item_archived', ?, ?)",
                (int(item_id), str(row["name"])),
            )
        return True

    def restore_item(self, item_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT name FROM items
                WHERE id = ? AND active = 0 AND merged_into_item_id IS NULL
                """,
                (int(item_id),),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                """
                UPDATE items
                SET active = 1,
                    archived_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(item_id),),
            )
            conn.execute(
                "INSERT INTO events(event_type, item_id, note) "
                "VALUES ('item_restored', ?, ?)",
                (int(item_id), str(row["name"])),
            )
        return True

    def resolve_item_id(self, item_id: int) -> int | None:
        with self.connect() as conn:
            current_id = int(item_id)
            seen: set[int] = set()
            while current_id not in seen:
                seen.add(current_id)
                row = conn.execute(
                    "SELECT id, merged_into_item_id FROM items WHERE id = ?",
                    (current_id,),
                ).fetchone()
                if row is None:
                    return None
                if row["merged_into_item_id"] is None:
                    return int(row["id"])
                current_id = int(row["merged_into_item_id"])
        return None

    def item_merge_preview(
        self, *, source_item_id: int, target_item_id: int
    ) -> dict[str, object]:
        source_id = int(source_item_id)
        target_id = int(target_item_id)
        if source_id == target_id:
            raise ValueError("source and canonical item must be different")
        with self.connect() as conn:
            items = conn.execute(
                """
                SELECT id, name, active, merged_into_item_id
                FROM items WHERE id IN (?, ?)
                """,
                (source_id, target_id),
            ).fetchall()
            by_id = {int(row["id"]): row for row in items}
            source = by_id.get(source_id)
            target = by_id.get(target_id)
            if source is None or target is None:
                raise ValueError("item not found")
            if not bool(source["active"]) or source["merged_into_item_id"] is not None:
                raise ValueError("source item is not active")
            if not bool(target["active"]) or target["merged_into_item_id"] is not None:
                raise ValueError("canonical item is not active")
            rows = conn.execute(
                """
                SELECT item_id, bin_id, quantity, unit
                FROM inventory WHERE item_id IN (?, ?)
                ORDER BY bin_id, item_id
                """,
                (source_id, target_id),
            ).fetchall()
            source_rows = {
                str(row["bin_id"]): row for row in rows if int(row["item_id"]) == source_id
            }
            target_rows = {
                str(row["bin_id"]): row for row in rows if int(row["item_id"]) == target_id
            }
            placements: list[dict[str, object]] = []
            conflicts: list[dict[str, object]] = []
            for location_id, source_row in source_rows.items():
                target_row = target_rows.get(location_id)
                if target_row is not None and str(target_row["unit"]).casefold() != str(
                    source_row["unit"]
                ).casefold():
                    conflicts.append(
                        {
                            "location_id": location_id,
                            "source_unit": str(source_row["unit"]),
                            "target_unit": str(target_row["unit"]),
                        }
                    )
                    continue
                quantity = float(source_row["quantity"])
                if target_row is not None:
                    quantity += float(target_row["quantity"])
                placements.append(
                    {
                        "location_id": location_id,
                        "quantity": quantity,
                        "unit": str(target_row["unit"] if target_row else source_row["unit"]),
                        "combined": target_row is not None,
                    }
                )
            alias_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM aliases WHERE item_id = ?", (source_id,)
                ).fetchone()[0]
            )
            photo_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM photos WHERE item_id = ?", (source_id,)
                ).fetchone()[0]
            )
        return {
            "source": {"item_id": source_id, "name": str(source["name"])},
            "target": {"item_id": target_id, "name": str(target["name"])},
            "placements": placements,
            "conflicts": conflicts,
            "aliases_transferred": alias_count + 1,
            "photos_transferred": photo_count,
            "can_merge": not conflicts,
        }

    def merge_item(self, *, source_item_id: int, target_item_id: int) -> dict[str, object]:
        preview = self.item_merge_preview(
            source_item_id=source_item_id, target_item_id=target_item_id
        )
        if preview["conflicts"]:
            raise ValueError("items use different units in the same location")
        source_id = int(source_item_id)
        target_id = int(target_item_id)
        with self.connect() as conn:
            source = conn.execute(
                "SELECT * FROM items WHERE id = ?", (source_id,)
            ).fetchone()
            target = conn.execute(
                "SELECT * FROM items WHERE id = ?", (target_id,)
            ).fetchone()
            if source is None or target is None:
                raise ValueError("item not found")
            if (
                not bool(source["active"])
                or source["merged_into_item_id"] is not None
                or not bool(target["active"])
                or target["merged_into_item_id"] is not None
            ):
                raise ValueError("item merge state changed; review the merge again")
            conflict = conn.execute(
                """
                SELECT source.bin_id
                FROM inventory source
                JOIN inventory canonical ON canonical.bin_id = source.bin_id
                WHERE source.item_id = ?
                  AND canonical.item_id = ?
                  AND lower(source.unit) <> lower(canonical.unit)
                LIMIT 1
                """,
                (source_id, target_id),
            ).fetchone()
            if conflict is not None:
                raise ValueError("items use different units in the same location")
            source_rows = conn.execute(
                "SELECT * FROM inventory WHERE item_id = ?", (source_id,)
            ).fetchall()
            for row in source_rows:
                location_id = str(row["bin_id"])
                existing = conn.execute(
                    "SELECT * FROM inventory WHERE item_id = ? AND bin_id = ?",
                    (target_id, location_id),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "UPDATE inventory SET item_id = ? WHERE item_id = ? AND bin_id = ?",
                        (target_id, source_id, location_id),
                    )
                    continue
                conn.execute(
                    """
                    UPDATE inventory
                    SET quantity = ?,
                        needs_review = ?,
                        review_reason = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE item_id = ? AND bin_id = ?
                    """,
                    (
                        float(existing["quantity"]) + float(row["quantity"]),
                        int(bool(existing["needs_review"]) or bool(row["needs_review"])),
                        str(existing["review_reason"] or row["review_reason"] or ""),
                        target_id,
                        location_id,
                    ),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO inventory_review_photos(item_id, bin_id, photo_id)
                    SELECT ?, bin_id, photo_id
                    FROM inventory_review_photos
                    WHERE item_id = ? AND bin_id = ?
                    """,
                    (target_id, source_id, location_id),
                )
                conn.execute(
                    "DELETE FROM inventory_review_photos WHERE item_id = ? AND bin_id = ?",
                    (source_id, location_id),
                )
                conn.execute(
                    "DELETE FROM inventory WHERE item_id = ? AND bin_id = ?",
                    (source_id, location_id),
                )
            conn.execute(
                "UPDATE inventory_review_photos SET item_id = ? WHERE item_id = ?",
                (target_id, source_id),
            )
            conn.execute(
                "UPDATE photos SET item_id = ? WHERE item_id = ?", (target_id, source_id)
            )
            source_aliases = [
                str(row["alias"])
                for row in conn.execute(
                    "SELECT alias FROM aliases WHERE item_id = ?", (source_id,)
                ).fetchall()
            ]
            for alias in [str(source["name"]), *source_aliases]:
                if alias.casefold() != str(target["name"]).casefold():
                    conn.execute(
                        "INSERT OR IGNORE INTO aliases(item_id, alias) VALUES (?, ?)",
                        (target_id, alias),
                    )
            conn.execute("DELETE FROM aliases WHERE item_id = ?", (source_id,))
            conn.execute(
                """
                UPDATE items
                SET description = CASE WHEN description = '' THEN ? ELSE description END,
                    category = CASE WHEN category = '' THEN ? ELSE category END,
                    size_class = CASE WHEN size_class = '' THEN ? ELSE size_class END,
                    size_source = CASE WHEN size_class = '' THEN ? ELSE size_source END,
                    size_confidence = CASE WHEN size_class = '' THEN ? ELSE size_confidence END,
                    dimensions_json = CASE
                        WHEN dimensions_json IS NULL OR dimensions_json = '' THEN ?
                        ELSE dimensions_json
                    END,
                    home_location_id = CASE
                        WHEN home_location_id = '' THEN ? ELSE home_location_id
                    END,
                    last_seen_location_id = CASE
                        WHEN last_seen_location_id = '' THEN ? ELSE last_seen_location_id
                    END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    str(source["description"]),
                    str(source["category"]),
                    str(source["size_class"]),
                    str(source["size_source"]),
                    float(source["size_confidence"]),
                    source["dimensions_json"],
                    str(source["home_location_id"]),
                    str(source["last_seen_location_id"]),
                    target_id,
                ),
            )
            conn.execute(
                "UPDATE events SET item_id = ? WHERE item_id = ?", (target_id, source_id)
            )
            conn.execute(
                """
                UPDATE items
                SET active = 0,
                    archived_at = CURRENT_TIMESTAMP,
                    merged_into_item_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (target_id, source_id),
            )
            conn.execute(
                """
                INSERT INTO events(event_type, item_id, note)
                VALUES ('item_merged', ?, ?)
                """,
                (target_id, f"{source['name']} merged into {target['name']}"),
            )
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
        aliases = aliases or []
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("item name is required")
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT name, dimensions_json FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if existing is None:
                raise ValueError("item not found")
            current_dimensions = parse_dimensions(existing["dimensions_json"])
            previous_inference = inferred_dimensions(str(existing["name"]))
            next_dimensions = current_dimensions
            if not current_dimensions or current_dimensions == previous_inference:
                next_dimensions = inferred_dimensions(clean_name) or None
            try:
                conn.execute(
                    """
                    UPDATE items
                    SET name = ?,
                        description = ?,
                        category = ?,
                        dimensions_json = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        clean_name,
                        description.strip(),
                        category.strip(),
                        json.dumps(next_dimensions, sort_keys=True)
                        if next_dimensions
                        else None,
                        item_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("another item already uses that name") from exc
            conn.execute("DELETE FROM aliases WHERE item_id = ?", (item_id,))
            for alias in aliases:
                clean_alias = alias.strip()
                if clean_alias:
                    conn.execute(
                        "INSERT OR IGNORE INTO aliases(item_id, alias) VALUES (?, ?)",
                        (item_id, clean_alias),
                    )
            conn.execute(
                "INSERT INTO events(event_type, item_id, note) VALUES ('item_update', ?, ?)",
                (item_id, clean_name),
            )

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
        # Cabinet bins normalize to "B###"; arbitrary location ids (pouches,
        # shelves) are stored verbatim so they round-trip with the locations tree.
        normalized_bin = (
            normalize_inventory_location_id(bin_id) if normalize_bin else bin_id
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO inventory(item_id, bin_id, quantity, unit, needs_review, review_reason)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id, bin_id) DO UPDATE SET
                    quantity = excluded.quantity,
                    unit = excluded.unit,
                    needs_review = excluded.needs_review,
                    review_reason = excluded.review_reason,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    item_id,
                    normalized_bin,
                    quantity,
                    unit.strip() or "each",
                    1 if needs_review else 0,
                    review_reason.strip() if needs_review else "",
                ),
            )
            conn.execute(
                """
                INSERT INTO events(event_type, item_id, bin_id, quantity, note)
                VALUES ('inventory_put', ?, ?, ?, ?)
                """,
                (
                    item_id,
                    normalized_bin,
                    quantity,
                    "needs_review" if needs_review else "",
                ),
            )
            conn.execute(
                """
                UPDATE items
                SET last_seen_location_id = ?,
                    home_location_id = CASE
                        WHEN home_location_id = '' THEN ?
                        ELSE home_location_id
                    END,
                    active = 1,
                    archived_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (normalized_bin, normalized_bin, item_id),
            )

    def import_inventory_rows_atomic(
        self,
        *,
        new_locations: list[Location],
        rows: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """Create reviewed paths and import inventory in one SQLite transaction."""
        from .locations import LocationStore

        location_store = LocationStore(self.path)
        imported: list[dict[str, object]] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            location_store.create_many_with_connection(conn, new_locations)
            for location in new_locations:
                conn.execute(
                    """
                    INSERT INTO events(event_type, bin_id, note)
                    VALUES ('location_created', ?, 'inventory CSV import')
                    """,
                    (location.id,),
                )

            for row in rows:
                clean_name = str(row.get("name") or "").strip()
                location_id = str(row.get("location_id") or "").strip()
                if not clean_name:
                    raise ValueError("item name is required")
                destination = conn.execute(
                    """
                    SELECT active, accepts_items
                    FROM locations
                    WHERE id = ?
                    """,
                    (location_id,),
                ).fetchone()
                if destination is None or not bool(destination["active"]):
                    raise ValueError(f"unknown or archived location: {location_id}")
                if not bool(destination["accepts_items"]):
                    raise ValueError(
                        f"location does not hold items directly: {location_id}"
                    )

                merged = conn.execute(
                    "SELECT merged_into_item_id FROM items WHERE name = ?",
                    (clean_name,),
                ).fetchone()
                merged_item_id = (
                    int(merged["merged_into_item_id"])
                    if merged is not None
                    and merged["merged_into_item_id"] is not None
                    else None
                )
                if merged_item_id is not None:
                    item_id = merged_item_id
                    aliases = [clean_name, *list(row.get("aliases") or [])]
                else:
                    dimensions = inferred_dimensions(clean_name)
                    conn.execute(
                        """
                        INSERT INTO items(name, description, category, dimensions_json)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(name) DO UPDATE SET
                            description = excluded.description,
                            category = excluded.category,
                            active = 1,
                            archived_at = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (
                            clean_name,
                            str(row.get("description") or "").strip(),
                            str(row.get("category") or "").strip(),
                            json.dumps(dimensions, sort_keys=True)
                            if dimensions
                            else None,
                        ),
                    )
                    item_id = int(
                        conn.execute(
                            "SELECT id FROM items WHERE name = ?", (clean_name,)
                        ).fetchone()["id"]
                    )
                    aliases = list(row.get("aliases") or [])
                for alias in aliases:
                    clean_alias = str(alias).strip()
                    if clean_alias:
                        conn.execute(
                            "INSERT OR IGNORE INTO aliases(item_id, alias) VALUES (?, ?)",
                            (item_id, clean_alias),
                        )
                conn.execute(
                    "INSERT INTO events(event_type, item_id, note) VALUES ('item_upsert', ?, ?)",
                    (item_id, clean_name),
                )

                quantity = float(row.get("quantity") or 0)
                unit = str(row.get("unit") or "each").strip() or "each"
                conn.execute(
                    """
                    INSERT INTO inventory(
                        item_id, bin_id, quantity, unit, needs_review, review_reason
                    )
                    VALUES (?, ?, ?, ?, 0, '')
                    ON CONFLICT(item_id, bin_id) DO UPDATE SET
                        quantity = excluded.quantity,
                        unit = excluded.unit,
                        needs_review = 0,
                        review_reason = '',
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (item_id, location_id, quantity, unit),
                )
                conn.execute(
                    """
                    INSERT INTO events(event_type, item_id, bin_id, quantity, note)
                    VALUES ('inventory_put', ?, ?, ?, '')
                    """,
                    (item_id, location_id, quantity),
                )
                conn.execute(
                    """
                    UPDATE items
                    SET last_seen_location_id = ?,
                        home_location_id = CASE
                            WHEN home_location_id = '' THEN ?
                            ELSE home_location_id
                        END,
                        active = 1,
                        archived_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (location_id, location_id, item_id),
                )
                imported.append(
                    {
                        "item_id": item_id,
                        "name": clean_name,
                        "location_id": location_id,
                        "quantity": quantity,
                        "unit": unit,
                    }
                )
        return imported

    def search(self, query: str, *, limit: int = 20) -> list[SearchResult]:
        terms = [term for term in query.lower().split() if term]
        if not terms:
            return []
        where_parts: list[str] = []
        params: list[str] = []
        for term in terms:
            pattern = f"%{term}%"
            where_parts.append(
                """
                (
                    lower(i.name) LIKE ?
                    OR lower(i.description) LIKE ?
                    OR lower(i.category) LIKE ?
                    OR lower(COALESCE(a.alias, '')) LIKE ?
                )
                """
            )
            params.extend([pattern, pattern, pattern, pattern])
        sql = f"""
            SELECT
                i.id,
                i.name,
                i.description,
                i.category,
                i.size_class,
                i.size_source,
                i.size_confidence,
                i.dimensions_json,
                i.home_location_id,
                i.last_seen_location_id,
                inv.bin_id,
                inv.quantity,
                inv.unit,
                inv.needs_review,
                inv.review_reason,
                GROUP_CONCAT(DISTINCT a.alias) AS aliases,
                GROUP_CONCAT(DISTINCT p.id) AS photo_ids,
                GROUP_CONCAT(DISTINCT rp.photo_id) AS review_photo_ids
            FROM items i
            JOIN inventory inv ON inv.item_id = i.id
            LEFT JOIN aliases a ON a.item_id = i.id
            LEFT JOIN photos p ON p.item_id = i.id
            LEFT JOIN inventory_review_photos rp ON
                rp.item_id = i.id AND rp.bin_id = inv.bin_id
            WHERE i.active = 1 AND {' AND '.join(where_parts)}
            GROUP BY i.id, inv.bin_id
            ORDER BY i.name, inv.bin_id
            LIMIT ?
        """
        params.append(str(limit))
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [result_from_row(row) for row in rows]

    def list_inventory(self, *, include_archived: bool = False) -> list[SearchResult]:
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    i.id,
                    i.name,
                    i.description,
                    i.category,
                    i.size_class,
                    i.size_source,
                    i.size_confidence,
                    i.dimensions_json,
                    i.home_location_id,
                    i.last_seen_location_id,
                    i.active,
                    i.archived_at,
                    inv.bin_id,
                    inv.quantity,
                    inv.unit,
                    inv.needs_review,
                    inv.review_reason,
                    GROUP_CONCAT(DISTINCT a.alias) AS aliases,
                    GROUP_CONCAT(DISTINCT p.id) AS photo_ids,
                    GROUP_CONCAT(DISTINCT rp.photo_id) AS review_photo_ids
                FROM items i
                JOIN inventory inv ON inv.item_id = i.id
                LEFT JOIN aliases a ON a.item_id = i.id
                LEFT JOIN photos p ON p.item_id = i.id
                LEFT JOIN inventory_review_photos rp ON
                    rp.item_id = i.id AND rp.bin_id = inv.bin_id
                {"WHERE 1 = 1" if include_archived else "WHERE i.active = 1"}
                GROUP BY i.id, inv.bin_id
                ORDER BY inv.bin_id, i.name
                """
            ).fetchall()
        return [result_from_row(row) for row in rows]

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
        clean_query = re.sub(r"\s+", " ", query).strip().lower()[:200]
        clean_category = category.strip().lower()[:100]
        clean_location_ids = sorted(
            {
                str(location_id).strip()
                for location_id in location_ids or set()
                if str(location_id).strip()
            }
        )
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
        active = 0 if archived else 1

        with self.connect() as conn:
            has_locations = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'locations'"
                ).fetchone()
                is not None
            )
            location_cte = ""
            location_value = "inv.bin_id"
            if has_locations:
                location_cte = """
                    RECURSIVE location_paths(id, path, trail) AS (
                        SELECT
                            id,
                            CASE WHEN trim(label) = '' THEN id ELSE label END,
                            '/' || id || '/'
                        FROM locations
                        WHERE parent_id IS NULL
                        UNION ALL
                        SELECT
                            child.id,
                            parent.path || ' › ' ||
                                CASE WHEN trim(child.label) = ''
                                    THEN child.id ELSE child.label END,
                            parent.trail || child.id || '/'
                        FROM locations child
                        JOIN location_paths parent ON child.parent_id = parent.id
                        WHERE instr(parent.trail, '/' || child.id || '/') = 0
                    ),
                """
                location_value = "COALESCE(lp.path, inv.bin_id)"

            conditions = ["i.active = ?"]
            params: list[object] = [active]
            if clean_category:
                conditions.append("lower(trim(i.category)) = ?")
                params.append(clean_category)
            if clean_location_ids:
                placeholders = ",".join("?" for _ in clean_location_ids)
                conditions.append(
                    "EXISTS (SELECT 1 FROM inventory location_inventory "
                    "WHERE location_inventory.item_id = i.id "
                    f"AND location_inventory.bin_id IN ({placeholders}))"
                )
                params.extend(clean_location_ids)
            if clean_query:
                pattern = f"%{clean_query}%"
                conditions.append(
                    f"""
                    (
                        lower(i.name) LIKE ?
                        OR lower(i.category) LIKE ?
                        OR lower(i.description) LIKE ?
                        OR EXISTS (
                            SELECT 1
                            FROM aliases search_alias
                            WHERE search_alias.item_id = i.id
                              AND lower(search_alias.alias) LIKE ?
                        )
                        OR EXISTS (
                            SELECT 1
                            FROM inventory search_inventory
                            {"LEFT JOIN location_paths search_path ON search_path.id = search_inventory.bin_id" if has_locations else ""}
                            WHERE search_inventory.item_id = i.id
                              AND lower({"COALESCE(search_path.path, search_inventory.bin_id)" if has_locations else "search_inventory.bin_id"}) LIKE ?
                        )
                    )
                    """
                )
                params.extend([pattern] * 5)
            if clean_scope == "review":
                conditions.append(
                    "EXISTS (SELECT 1 FROM inventory review_inventory "
                    "WHERE review_inventory.item_id = i.id "
                    "AND review_inventory.needs_review = 1)"
                )
            elif clean_scope in {"photos", "no-photo"}:
                photo_exists = """
                    (
                        EXISTS (
                            SELECT 1 FROM photos item_photo
                            WHERE item_photo.item_id = i.id
                        )
                        OR EXISTS (
                            SELECT 1
                            FROM inventory review_inventory
                            JOIN inventory_review_photos review_photo
                              ON review_photo.item_id = review_inventory.item_id
                             AND review_photo.bin_id = review_inventory.bin_id
                            WHERE review_inventory.item_id = i.id
                        )
                    )
                """
                conditions.append(photo_exists if clean_scope == "photos" else f"NOT {photo_exists}")
            elif clean_scope == "uncategorized":
                conditions.append("trim(i.category) = ''")

            where = " AND ".join(conditions)
            eligible = f"""
                eligible AS (
                    SELECT
                        i.id,
                        i.name,
                        i.category,
                        COALESCE((
                            SELECT SUM(quantity)
                            FROM inventory quantity_inventory
                            WHERE quantity_inventory.item_id = i.id
                        ), 0) AS total_quantity,
                        COALESCE((
                            SELECT MIN({location_value})
                            FROM inventory inv
                            {"LEFT JOIN location_paths lp ON lp.id = inv.bin_id" if has_locations else ""}
                            WHERE inv.item_id = i.id
                        ), '') AS first_location
                    FROM items i
                    WHERE {where}
                      AND EXISTS (
                          SELECT 1 FROM inventory present_inventory
                          WHERE present_inventory.item_id = i.id
                      )
                )
            """
            with_clause = f"WITH {location_cte}{eligible}"
            total = int(
                conn.execute(
                    f"{with_clause} SELECT COUNT(*) FROM eligible",
                    params,
                ).fetchone()[0]
            )
            order_by = {
                "name": "name COLLATE MAGPIE_NATURAL, id",
                "category": (
                    "CASE WHEN trim(category) = '' THEN 'Uncategorized' ELSE category END "
                    "COLLATE MAGPIE_NATURAL, name COLLATE MAGPIE_NATURAL, id"
                ),
                "location": (
                    "first_location COLLATE MAGPIE_NATURAL, "
                    "name COLLATE MAGPIE_NATURAL, id"
                ),
                "quantity": (
                    "total_quantity DESC, name COLLATE MAGPIE_NATURAL, id"
                ),
            }[clean_sort]
            page_ids = [
                int(row["id"])
                for row in conn.execute(
                    f"""
                    {with_clause}
                    SELECT id
                    FROM eligible
                    ORDER BY {order_by}
                    LIMIT ? OFFSET ?
                    """,
                    [*params, clean_limit, clean_offset],
                ).fetchall()
            ]

            metric_location_filter = ""
            metric_params: list[object] = [active]
            if clean_location_ids:
                metric_placeholders = ",".join("?" for _ in clean_location_ids)
                metric_location_filter = (
                    f" AND inv.bin_id IN ({metric_placeholders})"
                )
                metric_params.extend(clean_location_ids)
            metric_row = conn.execute(
                f"""
                SELECT
                    COUNT(DISTINCT i.id) AS items,
                    COUNT(inv.item_id) AS placements,
                    COUNT(DISTINCT inv.bin_id) AS locations,
                    COUNT(DISTINCT CASE WHEN inv.needs_review = 1 THEN i.id END)
                        AS need_review
                FROM items i
                JOIN inventory inv ON inv.item_id = i.id
                WHERE i.active = ?{metric_location_filter}
                """,
                metric_params,
            ).fetchone()
            category_location_filter = ""
            category_params: list[object] = [active]
            if clean_location_ids:
                category_placeholders = ",".join("?" for _ in clean_location_ids)
                category_location_filter = (
                    f" AND inv.bin_id IN ({category_placeholders})"
                )
                category_params.extend(clean_location_ids)
            categories = [
                str(row["category"])
                for row in conn.execute(
                    f"""
                    SELECT DISTINCT trim(i.category) AS category
                    FROM items i
                    JOIN inventory inv ON inv.item_id = i.id
                    WHERE i.active = ?
                      AND trim(i.category) <> ''
                      {category_location_filter}
                    ORDER BY category COLLATE NOCASE
                    """,
                    category_params,
                ).fetchall()
            ]
            if not page_ids:
                page_items: list[SearchResult] = []
            else:
                placeholders = ",".join("?" for _ in page_ids)
                rows = conn.execute(
                    f"""
                    SELECT
                        i.id,
                        i.name,
                        i.description,
                        i.category,
                        i.size_class,
                        i.size_source,
                        i.size_confidence,
                        i.dimensions_json,
                        i.home_location_id,
                        i.last_seen_location_id,
                        i.active,
                        i.archived_at,
                        inv.bin_id,
                        inv.quantity,
                        inv.unit,
                        inv.needs_review,
                        inv.review_reason,
                        GROUP_CONCAT(DISTINCT a.alias) AS aliases,
                        GROUP_CONCAT(DISTINCT p.id) AS photo_ids,
                        GROUP_CONCAT(DISTINCT rp.photo_id) AS review_photo_ids
                    FROM items i
                    JOIN inventory inv ON inv.item_id = i.id
                    LEFT JOIN aliases a ON a.item_id = i.id
                    LEFT JOIN photos p ON p.item_id = i.id
                    LEFT JOIN inventory_review_photos rp ON
                        rp.item_id = i.id AND rp.bin_id = inv.bin_id
                    WHERE i.id IN ({placeholders})
                    GROUP BY i.id, inv.bin_id
                    """,
                    page_ids,
                ).fetchall()
                position = {
                    item_id: index for index, item_id in enumerate(page_ids)
                }
                page_items = sorted(
                    (result_from_row(row) for row in rows),
                    key=lambda result: (
                        position[result.item_id],
                        _natural_text_key(result.bin_id),
                    ),
                )

        return InventoryCatalogPage(
            items=page_items,
            total=total,
            metrics={
                "items": int(metric_row["items"] or 0),
                "placements": int(metric_row["placements"] or 0),
                "locations": int(metric_row["locations"] or 0),
                "need_review": int(metric_row["need_review"] or 0),
            },
            categories=categories,
        )

    def bin_inventory(self, bin_id: str) -> list[SearchResult]:
        normalized_bin = normalize_inventory_location_id(bin_id)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    i.id,
                    i.name,
                    i.description,
                    i.category,
                    i.size_class,
                    i.size_source,
                    i.size_confidence,
                    i.dimensions_json,
                    i.home_location_id,
                    i.last_seen_location_id,
                    inv.bin_id,
                    inv.quantity,
                    inv.unit,
                    inv.needs_review,
                    inv.review_reason,
                    GROUP_CONCAT(DISTINCT a.alias) AS aliases,
                    GROUP_CONCAT(DISTINCT p.id) AS photo_ids,
                    GROUP_CONCAT(DISTINCT rp.photo_id) AS review_photo_ids
                FROM items i
                JOIN inventory inv ON inv.item_id = i.id
                LEFT JOIN aliases a ON a.item_id = i.id
                LEFT JOIN photos p ON p.item_id = i.id
                LEFT JOIN inventory_review_photos rp ON
                    rp.item_id = i.id AND rp.bin_id = inv.bin_id
                WHERE inv.bin_id = ? AND i.active = 1
                GROUP BY i.id, inv.bin_id
                ORDER BY i.name
                """,
                (normalized_bin,),
            ).fetchall()
        return [result_from_row(row) for row in rows]

    def review_queue(self, *, limit: int = 100) -> list[SearchResult]:
        clean_limit = max(1, min(int(limit), 500))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    i.id,
                    i.name,
                    i.description,
                    i.category,
                    i.size_class,
                    i.size_source,
                    i.size_confidence,
                    i.dimensions_json,
                    i.home_location_id,
                    i.last_seen_location_id,
                    inv.bin_id,
                    inv.quantity,
                    inv.unit,
                    inv.needs_review,
                    inv.review_reason,
                    GROUP_CONCAT(DISTINCT a.alias) AS aliases,
                    GROUP_CONCAT(DISTINCT p.id) AS photo_ids,
                    GROUP_CONCAT(DISTINCT rp.photo_id) AS review_photo_ids
                FROM items i
                JOIN inventory inv ON inv.item_id = i.id
                LEFT JOIN aliases a ON a.item_id = i.id
                LEFT JOIN photos p ON p.item_id = i.id
                LEFT JOIN inventory_review_photos rp ON
                    rp.item_id = i.id AND rp.bin_id = inv.bin_id
                WHERE inv.needs_review = 1 AND i.active = 1
                GROUP BY i.id, inv.bin_id
                ORDER BY inv.updated_at DESC, inv.bin_id, i.name
                LIMIT ?
                """,
                (clean_limit,),
            ).fetchall()
        return [result_from_row(row) for row in rows]

    def mark_reviewed(self, *, item_id: int, bin_id: str) -> None:
        clean_bin = bin_id.strip()
        normalized_bin = normalize_inventory_location_id(clean_bin)
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT bin_id
                FROM inventory
                WHERE item_id = ? AND bin_id IN (?, ?)
                ORDER BY CASE WHEN bin_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (item_id, clean_bin, normalized_bin, clean_bin),
            ).fetchone()
            target_bin = str(existing["bin_id"]) if existing is not None else normalized_bin
            conn.execute(
                """
                UPDATE inventory
                SET needs_review = 0,
                    review_reason = '',
                    updated_at = CURRENT_TIMESTAMP
                WHERE item_id = ? AND bin_id = ?
                """,
                (item_id, target_bin),
            )
            conn.execute(
                """
                INSERT INTO events(event_type, item_id, bin_id, note)
                VALUES ('inventory_reviewed', ?, ?, '')
                """,
                (item_id, target_bin),
            )

    def mark_needs_review(self, *, item_id: int, bin_id: str, reason: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE inventory
                SET needs_review = 1,
                    review_reason = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE item_id = ? AND bin_id = ?
                """,
                (reason.strip(), item_id, bin_id),
            )
            conn.execute(
                """
                INSERT INTO events(event_type, item_id, bin_id, note)
                VALUES ('inventory_needs_review', ?, ?, '')
                """,
                (item_id, bin_id),
            )

    def apply_location_audit(
        self,
        *,
        location_id: str,
        location_ids: list[str],
        placements: list[dict[str, object]],
        verification_note: str,
    ) -> list[dict[str, object]]:
        if len(placements) > 100:
            raise ValueError("a contents check can include at most 100 placements")
        subtree = sorted({str(value).strip() for value in location_ids if str(value).strip()})
        if not subtree:
            raise ValueError("contents check location subtree is empty")
        requested: dict[tuple[int, str], dict[str, object]] = {}
        for placement in placements:
            key = (
                int(placement.get("item_id") or 0),
                str(placement.get("location_id") or "").strip(),
            )
            if key[0] <= 0 or not key[1] or key[1] not in subtree:
                raise ValueError("contents check contains an invalid placement")
            if key in requested:
                raise ValueError("contents check contains a duplicate placement")
            if placement.get("status") not in {"present", "missing"}:
                raise ValueError("contents check status must be present or missing")
            requested[key] = placement

        placeholders = ",".join("?" for _ in subtree)
        changes: list[dict[str, object]] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT inv.item_id, inv.bin_id, inv.quantity, inv.unit,
                       inv.needs_review, inv.review_reason
                FROM inventory inv
                JOIN items item ON item.id = inv.item_id
                WHERE inv.bin_id IN ({placeholders}) AND item.active = 1
                ORDER BY inv.bin_id, inv.item_id
                """,
                subtree,
            ).fetchall()
            current = {
                (int(row["item_id"]), str(row["bin_id"])): row for row in rows
            }
            if set(current) != set(requested):
                raise InventoryAuditConflictError(
                    "inventory changed while this contents check was open"
                )
            for key, placement in requested.items():
                row = current[key]
                if (
                    float(row["quantity"])
                    != float(placement.get("expected_quantity") or 0)
                    or str(row["unit"]) != str(placement.get("expected_unit") or "")
                    or bool(row["needs_review"])
                    != bool(placement.get("expected_needs_review"))
                ):
                    raise InventoryAuditConflictError(
                        "inventory changed while this contents check was open"
                    )

            for key, placement in requested.items():
                row = current[key]
                item_id, bin_id = key
                previous_needs_review = bool(row["needs_review"])
                previous_review_reason = str(row["review_reason"] or "")
                status = str(placement["status"])
                reason = str(placement.get("review_reason") or "").strip()
                needs_review = status == "missing"
                next_reason = reason if needs_review else ""
                if (
                    previous_needs_review == needs_review
                    and previous_review_reason == next_reason
                ):
                    continue
                conn.execute(
                    """
                    UPDATE inventory
                    SET needs_review = ?, review_reason = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE item_id = ? AND bin_id = ?
                    """,
                    (int(needs_review), next_reason, item_id, bin_id),
                )
                event_type = (
                    "inventory_needs_review" if needs_review else "inventory_reviewed"
                )
                conn.execute(
                    """
                    INSERT INTO events(event_type, item_id, bin_id, note)
                    VALUES (?, ?, ?, ?)
                    """,
                    (event_type, item_id, bin_id, next_reason),
                )
                changes.append(
                    {
                        "item_id": item_id,
                        "location_id": bin_id,
                        "status": status,
                        "previous_needs_review": previous_needs_review,
                        "previous_review_reason": previous_review_reason,
                    }
                )
            placement_counts: dict[str, int] = {candidate: 0 for candidate in subtree}
            for row in rows:
                row_location = str(row["bin_id"])
                placement_counts[row_location] = placement_counts.get(row_location, 0) + 1
            for verified_location_id in subtree:
                conn.execute(
                    """
                    INSERT INTO events(event_type, bin_id, quantity, note)
                    VALUES ('location_contents_verified', ?, ?, ?)
                    """,
                    (
                        verified_location_id,
                        float(
                            len(rows)
                            if verified_location_id == location_id
                            else placement_counts.get(verified_location_id, 0)
                        ),
                        verification_note.strip(),
                    ),
                )
        return changes

    def link_review_photo(self, *, item_id: int, bin_id: str, photo_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO inventory_review_photos(item_id, bin_id, photo_id)
                VALUES (?, ?, ?)
                """,
                (item_id, bin_id, photo_id),
            )

    def clear_bin_inventory(self, bin_id: str) -> int:
        normalized_bin = normalize_inventory_location_id(bin_id)
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM inventory WHERE bin_id = ?",
                (normalized_bin,),
            )
            removed = int(cursor.rowcount)
            conn.execute(
                """
                INSERT INTO events(event_type, bin_id, quantity, note)
                VALUES ('inventory_clear_bin', ?, ?, '')
                """,
                (normalized_bin, removed),
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
        normalized_bin = (
            normalize_inventory_location_id(bin_id) if normalize_bin else bin_id
        )
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM inventory WHERE item_id = ? AND bin_id = ?",
                (item_id, normalized_bin),
            )
            removed = int(cursor.rowcount)
            if removed:
                conn.execute(
                    """
                    INSERT INTO events(event_type, item_id, bin_id, note)
                    VALUES ('inventory_remove', ?, ?, ?)
                    """,
                    (item_id, normalized_bin, note.strip()),
                )
        return bool(removed)

    def move_inventory(
        self,
        *,
        item_id: int,
        from_bin: str,
        to_bin: str,
        normalize_bin: bool = False,
    ) -> bool:
        """Re-bin an item. If the destination already holds it, quantities merge.

        Returns False if there was nothing in the source bin to move."""
        src = (
            normalize_inventory_location_id(from_bin) if normalize_bin else from_bin
        )
        dst = normalize_inventory_location_id(to_bin) if normalize_bin else to_bin
        if src == dst:
            return False
        with self.connect() as conn:
            row = conn.execute(
                "SELECT quantity, unit, needs_review, review_reason FROM inventory WHERE item_id = ? AND bin_id = ?",
                (item_id, src),
            ).fetchone()
            if row is None:
                return False
            qty = float(row["quantity"])
            unit = str(row["unit"])
            needs_review = int(row["needs_review"])
            review_reason = str(row["review_reason"] or "")
            existing = conn.execute(
                "SELECT quantity FROM inventory WHERE item_id = ? AND bin_id = ?",
                (item_id, dst),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE inventory SET quantity = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE item_id = ? AND bin_id = ?",
                    (float(existing["quantity"]) + qty, item_id, dst),
                )
            else:
                conn.execute(
                    "INSERT INTO inventory(item_id, bin_id, quantity, unit, needs_review, review_reason) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (item_id, dst, qty, unit, needs_review, review_reason),
                )
            conn.execute(
                "DELETE FROM inventory WHERE item_id = ? AND bin_id = ?", (item_id, src)
            )
            conn.execute(
                "INSERT INTO events(event_type, item_id, bin_id, quantity, note) "
                "VALUES ('inventory_move', ?, ?, ?, ?)",
                (item_id, dst, qty, f"from {src}"),
            )
            conn.execute(
                """
                UPDATE items
                SET last_seen_location_id = ?,
                    home_location_id = CASE
                        WHEN home_location_id = '' THEN ?
                        ELSE home_location_id
                    END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (dst, dst, item_id),
            )
        return True

    def occupied_bin_ids(self) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT inv.bin_id
                FROM inventory inv
                JOIN items i ON i.id = inv.item_id
                WHERE i.active = 1
                """
            ).fetchall()
        return {str(row["bin_id"]) for row in rows}

    # --- compounding loop: learn from confirmations and corrections ---------

    def learn_alias(self, *, item_id: int, alias: str) -> None:
        """Teach the system a word for an item, so the next search hits it
        directly. This is how vocabulary bends to the user over time."""
        clean = alias.strip()
        if not clean:
            return
        for match in self.exact_match(clean):
            if match.item_id != item_id:
                if identities_conflict(clean, match.name):
                    raise ValueError(
                        f'alias "{clean}" conflicts with distinct item {match.name}'
                    )
                raise ValueError(f'alias "{clean}" already identifies {match.name}')
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO aliases(item_id, alias) VALUES (?, ?)",
                (item_id, clean),
            )
            conn.execute(
                "INSERT INTO events(event_type, item_id, note) "
                "VALUES ('alias_learned', ?, ?)",
                (item_id, clean),
            )

    def remove_alias(self, *, item_id: int, alias: str) -> bool:
        clean = alias.strip()
        if not clean:
            return False
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM aliases WHERE item_id = ? AND lower(alias) = lower(?)",
                (item_id, clean),
            )
            removed = int(cursor.rowcount)
            if removed:
                conn.execute(
                    "INSERT INTO events(event_type, item_id, note) "
                    "VALUES ('alias_removed', ?, ?)",
                    (item_id, clean),
                )
        return bool(removed)

    def exact_match(self, query: str) -> list[SearchResult]:
        """An item whose name or a learned alias equals the query exactly.

        Lets a find short-circuit the LLM once the system has been taught the
        user's word for something."""
        needle = query.strip().lower()
        if not needle:
            return []
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT i.id
                FROM items i
                LEFT JOIN aliases a ON a.item_id = i.id
                WHERE i.active = 1
                  AND (lower(i.name) = ? OR lower(a.alias) = ?)
                LIMIT 1
                """,
                (needle, needle),
            ).fetchone()
        if row is None:
            return []
        return self.item_inventory(int(row["id"]))

    def item_inventory(self, item_id: int) -> list[SearchResult]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    i.id, i.name, i.description, i.category,
                    i.size_class, i.size_source, i.size_confidence, i.dimensions_json,
                    i.home_location_id, i.last_seen_location_id,
                    inv.bin_id, inv.quantity, inv.unit, inv.needs_review, inv.review_reason,
                    GROUP_CONCAT(DISTINCT a.alias) AS aliases,
                    GROUP_CONCAT(DISTINCT p.id) AS photo_ids,
                    GROUP_CONCAT(DISTINCT rp.photo_id) AS review_photo_ids
                FROM items i
                JOIN inventory inv ON inv.item_id = i.id
                LEFT JOIN aliases a ON a.item_id = i.id
                LEFT JOIN photos p ON p.item_id = i.id
                LEFT JOIN inventory_review_photos rp ON
                    rp.item_id = i.id AND rp.bin_id = inv.bin_id
                WHERE i.id = ? AND i.active = 1
                GROUP BY i.id, inv.bin_id
                ORDER BY inv.bin_id
                """,
                (item_id,),
            ).fetchall()
        return [result_from_row(row) for row in rows]

    def items_in_location(self, location_id: str) -> list[SearchResult]:
        """Items stored directly at a location id (no bin normalization, so
        pouches/shelves work as well as cabinet bins)."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    i.id, i.name, i.description, i.category,
                    i.size_class, i.size_source, i.size_confidence, i.dimensions_json,
                    i.home_location_id, i.last_seen_location_id,
                    inv.bin_id, inv.quantity, inv.unit, inv.needs_review, inv.review_reason,
                    GROUP_CONCAT(DISTINCT a.alias) AS aliases,
                    GROUP_CONCAT(DISTINCT p.id) AS photo_ids,
                    GROUP_CONCAT(DISTINCT rp.photo_id) AS review_photo_ids
                FROM items i
                JOIN inventory inv ON inv.item_id = i.id
                LEFT JOIN aliases a ON a.item_id = i.id
                LEFT JOIN photos p ON p.item_id = i.id
                LEFT JOIN inventory_review_photos rp ON
                    rp.item_id = i.id AND rp.bin_id = inv.bin_id
                WHERE inv.bin_id = ? AND i.active = 1
                GROUP BY i.id, inv.bin_id
                ORDER BY i.name
                """,
                (location_id,),
            ).fetchall()
        return [result_from_row(row) for row in rows]

    def items_in_locations(self, location_ids: list[str]) -> list[SearchResult]:
        """All items stored anywhere in a set of locations (a container's whole
        subtree), each tagged with the exact bin id it lives in."""
        ids = [str(i) for i in location_ids]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    i.id, i.name, i.description, i.category,
                    i.size_class, i.size_source, i.size_confidence, i.dimensions_json,
                    i.home_location_id, i.last_seen_location_id,
                    inv.bin_id, inv.quantity, inv.unit, inv.needs_review, inv.review_reason,
                    GROUP_CONCAT(DISTINCT a.alias) AS aliases,
                    GROUP_CONCAT(DISTINCT p.id) AS photo_ids,
                    GROUP_CONCAT(DISTINCT rp.photo_id) AS review_photo_ids
                FROM items i
                JOIN inventory inv ON inv.item_id = i.id
                LEFT JOIN aliases a ON a.item_id = i.id
                LEFT JOIN photos p ON p.item_id = i.id
                LEFT JOIN inventory_review_photos rp ON
                    rp.item_id = i.id AND rp.bin_id = inv.bin_id
                WHERE inv.bin_id IN ({placeholders}) AND i.active = 1
                GROUP BY i.id, inv.bin_id
                ORDER BY inv.bin_id, i.name
                """,
                ids,
            ).fetchall()
        return [result_from_row(row) for row in rows]

    def items_in_locations_page(
        self,
        location_ids: list[str],
        *,
        query: str = "",
        limit: int = 30,
        offset: int = 0,
    ) -> tuple[list[SearchResult], int]:
        ids = [str(location_id) for location_id in location_ids if str(location_id)]
        if not ids:
            return [], 0
        clean_query = re.sub(r"\s+", " ", query).strip().lower()[:200]
        clean_limit = max(1, min(int(limit), 100))
        clean_offset = max(0, int(offset))
        placeholders = ",".join("?" for _ in ids)
        where = f"inv.bin_id IN ({placeholders}) AND i.active = 1"
        params: list[object] = list(ids)
        if clean_query:
            pattern = f"%{clean_query}%"
            where += """
                AND (
                    LOWER(i.name) LIKE ?
                    OR LOWER(i.category) LIKE ?
                    OR LOWER(i.description) LIKE ?
                    OR LOWER(inv.bin_id) LIKE ?
                    OR EXISTS (
                        SELECT 1 FROM aliases search_alias
                        WHERE search_alias.item_id = i.id
                          AND LOWER(search_alias.alias) LIKE ?
                    )
                )
            """
            params.extend([pattern] * 5)
        with self.connect() as conn:
            total = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM inventory inv
                    JOIN items i ON i.id = inv.item_id
                    WHERE {where}
                    """,
                    params,
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT
                    i.id, i.name, i.description, i.category,
                    i.size_class, i.size_source, i.size_confidence, i.dimensions_json,
                    i.home_location_id, i.last_seen_location_id,
                    inv.bin_id, inv.quantity, inv.unit, inv.needs_review, inv.review_reason,
                    GROUP_CONCAT(DISTINCT a.alias) AS aliases,
                    GROUP_CONCAT(DISTINCT p.id) AS photo_ids,
                    GROUP_CONCAT(DISTINCT rp.photo_id) AS review_photo_ids
                FROM items i
                JOIN inventory inv ON inv.item_id = i.id
                LEFT JOIN aliases a ON a.item_id = i.id
                LEFT JOIN photos p ON p.item_id = i.id
                LEFT JOIN inventory_review_photos rp ON
                    rp.item_id = i.id AND rp.bin_id = inv.bin_id
                WHERE {where}
                GROUP BY i.id, inv.bin_id
                ORDER BY inv.bin_id, i.name, i.id
                LIMIT ? OFFSET ?
                """,
                [*params, clean_limit, clean_offset],
            ).fetchall()
        return [result_from_row(row) for row in rows], total

    def location_item_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT inv.bin_id, COUNT(*) AS n
                FROM inventory inv
                JOIN items i ON i.id = inv.item_id
                WHERE i.active = 1
                GROUP BY inv.bin_id
                """
            ).fetchall()
        return {str(row["bin_id"]): int(row["n"]) for row in rows}

    def location_inventory_metrics(self) -> dict[str, dict[str, object]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT location.id AS location_id,
                       location.content_revision,
                       COUNT(item.id) AS item_count,
                       SUM(
                           CASE
                               WHEN item.id IS NOT NULL AND inv.needs_review = 1 THEN 1
                               ELSE 0
                           END
                       ) AS review_count
                FROM locations location
                LEFT JOIN inventory inv ON inv.bin_id = location.id
                LEFT JOIN items item ON item.id = inv.item_id AND item.active = 1
                GROUP BY location.id, location.content_revision
                """
            ).fetchall()
        return {
            str(row["location_id"]): {
                "item_count": int(row["item_count"] or 0),
                "review_count": int(row["review_count"] or 0),
                "content_revision": int(row["content_revision"] or 0),
            }
            for row in rows
        }

    def location_review_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT inv.bin_id, COUNT(*) AS n
                FROM inventory inv
                JOIN items i ON i.id = inv.item_id
                WHERE i.active = 1 AND inv.needs_review = 1
                GROUP BY inv.bin_id
                """
            ).fetchall()
        return {str(row["bin_id"]): int(row["n"]) for row in rows}

    def item_events(self, item_id: int, *, limit: int = 25) -> list[dict[str, object]]:
        clean_limit = max(1, min(int(limit), 100))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, event_type, item_id, bin_id, quantity, note, created_at
                FROM events
                WHERE item_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(item_id), clean_limit),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "event_type": str(row["event_type"]),
                "item_id": int(row["item_id"]) if row["item_id"] is not None else None,
                "bin_id": str(row["bin_id"] or ""),
                "quantity": float(row["quantity"]) if row["quantity"] is not None else None,
                "note": str(row["note"] or ""),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def suggest_home(self, name: str) -> str | None:
        """The durable home for this item, falling back to its last inventory row."""
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    CASE
                        WHEN i.home_location_id != '' THEN i.home_location_id
                        ELSE inv.bin_id
                    END AS location_id
                FROM items i
                JOIN inventory inv ON inv.item_id = i.id
                WHERE lower(i.name) = ?
                    OR ? LIKE '%' || lower(i.name) || '%'
                ORDER BY
                    CASE WHEN i.home_location_id != '' THEN 0 ELSE 1 END,
                    inv.updated_at DESC
                LIMIT 1
                """,
                (name.strip().lower(), name.strip().lower()),
            ).fetchone()
        return str(row["location_id"]) if row is not None else None

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
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO examples(command, action, context_json, kind) VALUES (?, ?, ?, ?)",
                (clean, action, json.dumps(context or {}, sort_keys=True), kind),
            )
            return int(cursor.lastrowid)

    def _set_example_review_status(
        self,
        example_id: int,
        *,
        status: str,
        event_type: str,
    ) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT command, kind FROM examples WHERE id = ?",
                (example_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                """
                UPDATE examples
                SET review_status = ?,
                    reviewed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, example_id),
            )
            conn.execute(
                "INSERT INTO events(event_type, note) VALUES (?, ?)",
                (event_type, f"{example_id}:{row['kind']}:{row['command']}"),
            )
        return True

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
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, command, action, context_json, kind, review_status, reviewed_at, created_at
                FROM examples
                WHERE id = ?
                """,
                (example_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def record_assistant_interaction(
        self,
        *,
        route: str,
        command: str,
        photo_ids: list[int] | None = None,
        response: dict[str, object] | None = None,
        status: str = "",
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO assistant_interactions(route, command, photo_ids, status, response_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    route.strip(),
                    command.strip(),
                    ",".join(str(photo_id) for photo_id in (photo_ids or [])),
                    status.strip(),
                    json.dumps(response or {}, sort_keys=True),
                ),
            )
            return int(cursor.lastrowid)

    def load_active_guidance_state(self) -> dict[str, object]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM active_guidance_state WHERE id = 1"
            ).fetchone()
        if row is None:
            return {"entries": [], "next_id": 1}
        try:
            state = json.loads(row["state_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            return {"entries": [], "next_id": 1}
        return state if isinstance(state, dict) else {"entries": [], "next_id": 1}

    def save_active_guidance_state(self, state: dict[str, object]) -> None:
        payload = json.dumps(state, sort_keys=True)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO active_guidance_state(id, state_json, updated_at)
                VALUES (1, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (payload,),
            )

    def assistant_interactions_after(self, after_id: int = 0, *, limit: int = 20) -> list[dict[str, object]]:
        clean_limit = max(1, min(int(limit), 100))
        with self.connect() as conn:
            if after_id > 0:
                rows = conn.execute(
                    """
                    SELECT id, route, command, photo_ids, status, response_json, created_at
                    FROM assistant_interactions
                    WHERE id > ?
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (int(after_id), clean_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, route, command, photo_ids, status, response_json, created_at
                    FROM (
                        SELECT id, route, command, photo_ids, status, response_json, created_at
                        FROM assistant_interactions
                        ORDER BY id DESC
                        LIMIT ?
                    )
                    ORDER BY id ASC
                    """,
                    (clean_limit,),
                ).fetchall()
        interactions: list[dict[str, object]] = []
        for row in rows:
            try:
                response = json.loads(row["response_json"] or "{}")
            except json.JSONDecodeError:
                response = {}
            photo_ids = [
                int(part)
                for part in str(row["photo_ids"] or "").split(",")
                if part.strip().isdigit()
            ]
            interactions.append(
                {
                    "id": row["id"],
                    "route": row["route"],
                    "command": row["command"],
                    "photo_ids": photo_ids,
                    "status": row["status"],
                    "response": response,
                    "created_at": row["created_at"],
                }
            )
        return interactions

    def assistant_interaction(self, interaction_id: int) -> dict[str, object] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, route, command, photo_ids, status, response_json, created_at
                FROM assistant_interactions
                WHERE id = ?
                """,
                (int(interaction_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            response = json.loads(row["response_json"] or "{}")
        except json.JSONDecodeError:
            response = {}
        photo_ids = [
            int(part)
            for part in str(row["photo_ids"] or "").split(",")
            if part.strip().isdigit()
        ]
        return {
            "id": int(row["id"]),
            "route": row["route"],
            "command": row["command"],
            "photo_ids": photo_ids,
            "status": row["status"],
            "response": response,
            "created_at": row["created_at"],
        }

    def assistant_interactions_by_ids(
        self, interaction_ids: list[int]
    ) -> list[dict[str, object]]:
        wanted = sorted({int(value) for value in interaction_ids if int(value) > 0})[:100]
        if not wanted:
            return []
        placeholders = ",".join("?" for _ in wanted)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, route, command, photo_ids, status, response_json, created_at
                FROM assistant_interactions
                WHERE id IN ({placeholders})
                ORDER BY id ASC
                """,
                wanted,
            ).fetchall()
        interactions: list[dict[str, object]] = []
        for row in rows:
            try:
                response = json.loads(row["response_json"] or "{}")
            except json.JSONDecodeError:
                response = {}
            interactions.append(
                {
                    "id": int(row["id"]),
                    "route": row["route"],
                    "command": row["command"],
                    "photo_ids": [
                        int(part)
                        for part in str(row["photo_ids"] or "").split(",")
                        if part.strip().isdigit()
                    ],
                    "status": row["status"],
                    "response": response,
                    "created_at": row["created_at"],
                }
            )
        return interactions

    def assistant_interaction_by_client_request_id(
        self,
        client_request_id: str,
        *,
        route: str = "",
    ) -> dict[str, object] | None:
        clean_id = client_request_id.strip()
        if not clean_id:
            return None
        route_filter = "AND route = ?" if route else ""
        parameters: tuple[object, ...] = (
            (clean_id, route) if route else (clean_id,)
        )
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT id, route, command, photo_ids, status, response_json, created_at
                FROM assistant_interactions
                WHERE CASE WHEN json_valid(response_json)
                        THEN json_extract(response_json, '$.client_request_id')
                      END = ?
                  {route_filter}
                ORDER BY id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        if row is None:
            return None
        try:
            response = json.loads(row["response_json"] or "{}")
        except json.JSONDecodeError:
            response = {}
        return {
            "id": int(row["id"]),
            "route": row["route"],
            "command": row["command"],
            "photo_ids": [
                int(part)
                for part in str(row["photo_ids"] or "").split(",")
                if part.strip().isdigit()
            ],
            "status": row["status"],
            "response": response,
            "created_at": row["created_at"],
        }

    def latest_pending_agent_plan(self) -> dict[str, object] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, route, command, photo_ids, status, response_json, created_at
                FROM assistant_interactions
                WHERE route IN (
                    '/assistant/agent',
                    '/assistant/chat',
                    '/assistant/revise',
                    '/mcp/propose_plan'
                )
                  AND status = 'pending'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        try:
            response = json.loads(row["response_json"] or "{}")
        except json.JSONDecodeError:
            response = {}
        plan = response.get("plan")
        if not isinstance(plan, list) or not plan:
            return None
        photo_ids = [
            int(part)
            for part in str(row["photo_ids"] or "").split(",")
            if part.strip().isdigit()
        ]
        return {
            "id": int(row["id"]),
            "route": row["route"],
            "command": row["command"],
            "photo_ids": photo_ids,
            "status": row["status"],
            "response": response,
            "created_at": row["created_at"],
        }

    def supersede_pending_agent_plans(self) -> int:
        """Close older proposals before recording a replacement plan."""
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE assistant_interactions
                SET status = 'superseded'
                WHERE status = 'pending'
                  AND route IN (
                    '/assistant/agent',
                    '/assistant/chat',
                    '/assistant/revise',
                    '/mcp/propose_plan'
                  )
                """
            )
            return int(cursor.rowcount)

    def latest_undoable_confirmed_plan(self) -> dict[str, object] | None:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, route, command, photo_ids, status, response_json, created_at
                FROM assistant_interactions
                WHERE route IN (
                    '/assistant/confirm', '/assistant/voice/confirm',
                    '/locations/create', '/locations/create-batch',
                    '/locations/create-structure',
                    '/locations/update', '/locations/move',
                    '/locations/archive', '/locations/restore',
                    '/inventory/home', '/inventory/manual',
                    '/rules/update', '/rules/disable', '/rules/restore',
                    '/storage/setup'
                )
                  AND status != 'undone'
                ORDER BY id DESC
                LIMIT 25
                """
            ).fetchall()
        for row in rows:
            try:
                response = json.loads(row["response_json"] or "{}")
            except json.JSONDecodeError:
                continue
            applied = response.get("applied")
            if not isinstance(applied, list) or not applied:
                continue
            photo_ids = [
                int(part)
                for part in str(row["photo_ids"] or "").split(",")
                if part.strip().isdigit()
            ]
            return {
                "id": int(row["id"]),
                "route": row["route"],
                "command": row["command"],
                "photo_ids": photo_ids,
                "status": row["status"],
                "response": response,
                "created_at": row["created_at"],
            }
        return None

    def set_assistant_interaction_status(self, interaction_id: int, status: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE assistant_interactions SET status = ? WHERE id = ?",
                (status.strip(), int(interaction_id)),
            )
            return bool(cursor.rowcount)

    def claim_assistant_interaction_confirmation(self, interaction_id: int) -> bool:
        """Atomically reserve a pending proposal for one confirmation request."""
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE assistant_interactions
                SET status = 'confirming'
                WHERE id = ? AND status = 'pending'
                """,
                (int(interaction_id),),
            )
            return bool(cursor.rowcount)

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
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO voice_attempts(
                    rms,
                    activity_rms,
                    transcript,
                    wake_word,
                    wake_heard,
                    awaiting_command,
                    command,
                    route,
                    query,
                    stt_model,
                    audio_threshold,
                    device_id,
                    runtime_version,
                    backend_response_json,
                    error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rms,
                    activity_rms,
                    transcript.strip(),
                    wake_word.strip(),
                    1 if wake_heard else 0,
                    1 if awaiting_command else 0,
                    command.strip(),
                    route.strip(),
                    query.strip(),
                    stt_model.strip(),
                    float(audio_threshold),
                    device_id.strip(),
                    runtime_version.strip(),
                    json.dumps(backend_response or {}, sort_keys=True),
                    error.strip(),
                ),
            )
            return int(cursor.lastrowid)

    def update_assistant_interaction_response(
        self,
        interaction_id: int,
        patch: dict[str, object],
    ) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT response_json FROM assistant_interactions WHERE id = ?",
                (int(interaction_id),),
            ).fetchone()
            if row is None:
                return False
            try:
                response = json.loads(row["response_json"] or "{}")
            except json.JSONDecodeError:
                response = {}
            if not isinstance(response, dict):
                response = {}
            response.update(patch)
            cursor = conn.execute(
                "UPDATE assistant_interactions SET response_json = ? WHERE id = ?",
                (json.dumps(response, sort_keys=True), int(interaction_id)),
            )
            return bool(cursor.rowcount)

    def recent_voice_attempts(self, *, limit: int = 20) -> list[dict[str, object]]:
        clean_limit = max(1, min(int(limit), 100))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, rms, activity_rms, transcript, wake_word, wake_heard, awaiting_command,
                       command, route, query, stt_model, audio_threshold, device_id, runtime_version,
                       backend_response_json, error, created_at
                FROM voice_attempts
                ORDER BY id DESC
                LIMIT ?
                """,
                (clean_limit,),
            ).fetchall()
        attempts: list[dict[str, object]] = []
        for row in reversed(rows):
            try:
                backend_response = json.loads(row["backend_response_json"] or "{}")
            except json.JSONDecodeError:
                backend_response = {}
            attempts.append(
                {
                    "id": row["id"],
                    "rms": row["rms"],
                    "activity_rms": row["activity_rms"],
                    "transcript": row["transcript"],
                    "wake_word": row["wake_word"],
                    "wake_heard": bool(row["wake_heard"]),
                    "awaiting_command": bool(row["awaiting_command"]),
                    "command": row["command"],
                    "route": row["route"],
                    "query": row["query"],
                    "stt_model": row["stt_model"],
                    "audio_threshold": row["audio_threshold"],
                    "device_id": row["device_id"],
                    "runtime_version": row["runtime_version"],
                    "backend_response": backend_response,
                    "error": row["error"],
                    "created_at": row["created_at"],
                }
            )
        return attempts

    def recent_voice_command_attempts(self, *, limit: int = 20) -> list[dict[str, object]]:
        """Return actual wake activations without ambient listener windows."""
        clean_limit = max(1, min(int(limit), 100))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, rms, activity_rms, transcript, wake_word, wake_heard, awaiting_command,
                       command, route, query, stt_model, audio_threshold, device_id, runtime_version,
                       backend_response_json, error, created_at
                FROM voice_attempts
                WHERE (
                    TRIM(command) <> ''
                    OR TRIM(query) <> ''
                    OR (wake_heard = 1 AND awaiting_command = 0)
                )
                  AND error <> 'awaiting_continuation'
                ORDER BY id DESC
                LIMIT ?
                """,
                (clean_limit,),
            ).fetchall()
        attempts: list[dict[str, object]] = []
        for row in reversed(rows):
            try:
                backend_response = json.loads(row["backend_response_json"] or "{}")
            except json.JSONDecodeError:
                backend_response = {}
            attempts.append(
                {
                    "id": row["id"],
                    "rms": row["rms"],
                    "activity_rms": row["activity_rms"],
                    "transcript": row["transcript"],
                    "wake_word": row["wake_word"],
                    "wake_heard": bool(row["wake_heard"]),
                    "awaiting_command": bool(row["awaiting_command"]),
                    "command": row["command"],
                    "route": row["route"],
                    "query": row["query"],
                    "stt_model": row["stt_model"],
                    "audio_threshold": row["audio_threshold"],
                    "device_id": row["device_id"],
                    "runtime_version": row["runtime_version"],
                    "backend_response": backend_response,
                    "error": row["error"],
                    "created_at": row["created_at"],
                }
            )
        return attempts

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
        with self.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if exists is None:
                raise ValueError("item not found")
            conn.execute(
                """
                UPDATE items
                SET size_class = ?,
                    size_source = ?,
                    size_confidence = ?,
                    dimensions_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    clean_size,
                    source.strip() or "user",
                    clamp_confidence(confidence),
                    json.dumps(dimensions) if dimensions else None,
                    item_id,
                ),
            )
            conn.execute(
                "INSERT INTO events(event_type, item_id, note) VALUES ('size_learned', ?, ?)",
                (item_id, clean_size),
            )

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
        with self.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if exists is None:
                raise ValueError("item not found")
            conn.execute(
                """
                UPDATE items
                SET size_class = ?,
                    size_source = ?,
                    size_confidence = ?,
                    dimensions_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    clean_size,
                    source.strip(),
                    clamp_confidence(confidence),
                    json.dumps(dimensions) if dimensions else None,
                    item_id,
                ),
            )
            conn.execute(
                "INSERT INTO events(event_type, item_id, note) VALUES ('size_restored', ?, ?)",
                (item_id, clean_size),
            )

    def update_item_home(self, *, item_id: int, location_id: str) -> None:
        clean_location = str(location_id).strip()
        with self.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if exists is None:
                raise ValueError("item not found")
            conn.execute(
                """
                UPDATE items
                SET home_location_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (clean_location, item_id),
            )
            conn.execute(
                """
                INSERT INTO events(event_type, item_id, bin_id, note)
                VALUES ('home_updated', ?, ?, '')
                """,
                (item_id, clean_location),
            )

    def restore_item_location_metadata(
        self,
        *,
        item_id: int,
        home_location_id: str,
        last_seen_location_id: str,
        active: bool,
        archived_at: str | None,
    ) -> None:
        clean_home = str(home_location_id).strip()
        clean_last_seen = str(last_seen_location_id).strip()
        restored_archived_at = None if active else str(archived_at or "").strip()
        with self.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM items WHERE id = ?", (int(item_id),)
            ).fetchone()
            if exists is None:
                raise ValueError("item not found")
            if not active and not restored_archived_at:
                conn.execute(
                    """
                    UPDATE items
                    SET home_location_id = ?,
                        last_seen_location_id = ?,
                        active = 0,
                        archived_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (clean_home, clean_last_seen, int(item_id)),
                )
            else:
                conn.execute(
                    """
                    UPDATE items
                    SET home_location_id = ?,
                        last_seen_location_id = ?,
                        active = ?,
                        archived_at = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        clean_home,
                        clean_last_seen,
                        1 if active else 0,
                        restored_archived_at,
                        int(item_id),
                    ),
                )
            conn.execute(
                """
                INSERT INTO events(event_type, item_id, bin_id, note)
                VALUES ('item_location_metadata_restored', ?, ?, ?)
                """,
                (
                    int(item_id),
                    clean_last_seen,
                    json.dumps(
                        {
                            "active": bool(active),
                            "home_location_id": clean_home,
                        },
                        sort_keys=True,
                    ),
                ),
            )

    def observe_confirmed_home(
        self,
        *,
        item_id: int,
        location_id: str,
        required_matches: int = 2,
        window: int = 3,
    ) -> dict[str, object]:
        clean_location = str(location_id).strip()
        if not clean_location:
            raise ValueError("location is required")
        required_matches = max(2, int(required_matches))
        window = max(required_matches, min(int(window), 10))
        with self.connect() as conn:
            item = conn.execute(
                "SELECT name, home_location_id, last_seen_location_id FROM items WHERE id = ?",
                (int(item_id),),
            ).fetchone()
            if item is None:
                raise ValueError("item not found")
            explicit_home = conn.execute(
                "SELECT 1 FROM events WHERE item_id = ? AND event_type = 'home_updated' LIMIT 1",
                (int(item_id),),
            ).fetchone() is not None
            cursor = conn.execute(
                "INSERT INTO events(event_type, item_id, bin_id, note) "
                "VALUES ('home_observed', ?, ?, 'confirmed')",
                (int(item_id), clean_location),
            )
            observation_id = int(cursor.lastrowid)
            observations = conn.execute(
                """
                SELECT id, bin_id
                FROM events
                WHERE item_id = ?
                  AND event_type = 'home_observed'
                  AND id NOT IN (
                      SELECT CAST(note AS INTEGER)
                      FROM events
                      WHERE item_id = ? AND event_type = 'home_observation_undone'
                  )
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(item_id), int(item_id), window),
            ).fetchall()
            matches = sum(
                1 for observation in observations
                if str(observation["bin_id"] or "") == clean_location
            )
            previous_home = str(item["home_location_id"] or "")
            promoted = (
                not explicit_home
                and clean_location != previous_home
                and matches >= required_matches
            )
            if promoted:
                conn.execute(
                    "UPDATE items SET home_location_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (clean_location, int(item_id)),
                )
                conn.execute(
                    "INSERT INTO events(event_type, item_id, bin_id, note) "
                    "VALUES ('home_learned', ?, ?, ?)",
                    (int(item_id), clean_location, f"{matches} of {len(observations)} confirmed"),
                )
        return {
            "home_observation": observation_id,
            "item_id": int(item_id),
            "item": str(item["name"]),
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
        with self.connect() as conn:
            observation = conn.execute(
                "SELECT item_id, bin_id FROM events "
                "WHERE id = ? AND event_type = 'home_observed'",
                (int(observation_id),),
            ).fetchone()
            if observation is None:
                return False
            already_undone = conn.execute(
                "SELECT 1 FROM events WHERE item_id = ? "
                "AND event_type = 'home_observation_undone' AND note = ? LIMIT 1",
                (int(observation["item_id"]), str(int(observation_id))),
            ).fetchone()
            if already_undone is not None:
                return False
            conn.execute(
                "INSERT INTO events(event_type, item_id, bin_id, note) "
                "VALUES ('home_observation_undone', ?, ?, ?)",
                (
                    int(observation["item_id"]),
                    str(observation["bin_id"] or ""),
                    str(int(observation_id)),
                ),
            )
        return True

    def clear_item_size(self, *, item_id: int) -> None:
        with self.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if exists is None:
                raise ValueError("item not found")
            conn.execute(
                """
                UPDATE items
                SET size_class = '',
                    size_source = '',
                    size_confidence = 0,
                    dimensions_json = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (item_id,),
            )
            conn.execute(
                "INSERT INTO events(event_type, item_id, note) VALUES ('size_cleared', ?, '')",
                (item_id,),
            )

    def item_size_class(self, name: str) -> str | None:
        needle = name.strip().lower()
        if not needle:
            return None
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT i.size_class
                FROM items i
                LEFT JOIN aliases a ON a.item_id = i.id
                WHERE (lower(i.name) = ? OR lower(a.alias) = ?)
                  AND i.size_class != ''
                LIMIT 1
                """,
                (needle, needle),
            ).fetchone()
        return str(row["size_class"]) if row is not None else None

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
        clean_target = target_location_id.strip()
        if not clean_rule:
            raise ValueError("rule text is required")
        if not clean_target:
            raise ValueError("target location is required")
        terms = normalize_rule_terms(trigger_terms or [clean_rule])
        if not terms:
            raise ValueError("at least one trigger term is required")
        clean_confidence = clamp_confidence(confidence)
        clean_source = source_command.strip()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM placement_rules
                WHERE active = 1 AND lower(rule_text) = lower(?)
                LIMIT 1
                """,
                (clean_rule,),
            ).fetchone()
            if row is not None:
                rule_id = int(row["id"])
                conn.execute(
                    """
                    UPDATE placement_rules
                    SET trigger_terms = ?,
                        target_location_id = ?,
                        source_command = ?,
                        confidence = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        ",".join(terms),
                        clean_target,
                        clean_source,
                        clean_confidence,
                        rule_id,
                    ),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO placement_rules(
                        rule_text, trigger_terms, target_location_id,
                        source_command, confidence
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        clean_rule,
                        ",".join(terms),
                        clean_target,
                        clean_source,
                        clean_confidence,
                    ),
                )
                rule_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO events(event_type, bin_id, note) VALUES ('rule_learned', ?, ?)",
                (clean_target, clean_rule),
            )
        return rule_id

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
        clean_confidence = clamp_confidence(confidence)
        clean_source = source_command.strip()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM size_rules
                WHERE active = 1 AND lower(rule_text) = lower(?)
                LIMIT 1
                """,
                (clean_rule,),
            ).fetchone()
            if row is not None:
                rule_id = int(row["id"])
                conn.execute(
                    """
                    UPDATE size_rules
                    SET trigger_terms = ?,
                        size_class = ?,
                        source_command = ?,
                        confidence = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        ",".join(terms),
                        clean_size,
                        clean_source,
                        clean_confidence,
                        rule_id,
                    ),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO size_rules(
                        rule_text, trigger_terms, size_class,
                        source_command, confidence
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        clean_rule,
                        ",".join(terms),
                        clean_size,
                        clean_source,
                        clean_confidence,
                    ),
                )
                rule_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO events(event_type, note) VALUES ('size_rule_learned', ?)",
                (clean_rule,),
            )
        return rule_id

    def size_rules(
        self, *, active_only: bool = True, limit: int = 50
    ) -> list[LearnedSizeRule]:
        where = "WHERE active = 1" if active_only else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, rule_text, trigger_terms, size_class,
                       source_command, confidence, active
                FROM size_rules
                {where}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [size_rule_from_row(row) for row in rows]

    def matching_size_rules(
        self, text: str, *, limit: int = 8
    ) -> list[LearnedSizeRule]:
        query_terms = set(normalize_rule_terms([text]))
        if not query_terms:
            return []
        scored: list[tuple[int, LearnedSizeRule]] = []
        for rule in self.size_rules(active_only=True, limit=100):
            overlap = query_terms & set(rule.trigger_terms)
            if overlap:
                scored.append((len(overlap), rule))
        scored.sort(key=lambda entry: (-entry[0], -entry[1].confidence, entry[1].id))
        return [rule for _, rule in scored[:limit]]

    def rules(self, *, active_only: bool = True, limit: int = 50) -> list[LearnedRule]:
        where = "WHERE active = 1" if active_only else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, rule_text, trigger_terms, target_location_id,
                       source_command, confidence, active
                FROM placement_rules
                {where}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [rule_from_row(row) for row in rows]

    def rule(self, rule_id: int, *, active_only: bool = True) -> LearnedRule | None:
        active_clause = "AND active = 1" if active_only else ""
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT id, rule_text, trigger_terms, target_location_id,
                       source_command, confidence, active
                FROM placement_rules
                WHERE id = ? {active_clause}
                """,
                (rule_id,),
            ).fetchone()
        return rule_from_row(row) if row else None

    def size_rule(
        self, rule_id: int, *, active_only: bool = True
    ) -> LearnedSizeRule | None:
        active_clause = "AND active = 1" if active_only else ""
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT id, rule_text, trigger_terms, size_class,
                       source_command, confidence, active
                FROM size_rules
                WHERE id = ? {active_clause}
                """,
                (rule_id,),
            ).fetchone()
        return size_rule_from_row(row) if row else None

    def matching_rules(self, text: str, *, limit: int = 8) -> list[LearnedRule]:
        query_terms = set(normalize_rule_terms([text]))
        if not query_terms:
            return []
        scored: list[tuple[int, LearnedRule]] = []
        for rule in self.rules(active_only=True, limit=100):
            rule_terms = set(rule.trigger_terms)
            overlap = query_terms & rule_terms
            if overlap:
                scored.append((len(overlap), rule))
        scored.sort(key=lambda entry: (-entry[0], -entry[1].confidence, entry[1].id))
        return [rule for _, rule in scored[:limit]]

    def rule_conflicts(self, text: str = "", *, limit: int = 20) -> list[dict[str, object]]:
        placement_rules = (
            self.matching_rules(text, limit=100)
            if text.strip()
            else self.rules(active_only=True, limit=100)
        )
        size_rules = (
            self.matching_size_rules(text, limit=100)
            if text.strip()
            else self.size_rules(active_only=True, limit=100)
        )
        conflicts = _placement_rule_conflicts(placement_rules, limit=limit)
        remaining = max(0, limit - len(conflicts))
        if remaining:
            conflicts.extend(_size_rule_conflicts(size_rules, limit=remaining))
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
        clean_rule = rule_text.strip()
        clean_target = target_location_id.strip()
        if not clean_rule:
            raise ValueError("rule text is required")
        if not clean_target:
            raise ValueError("target location is required")
        terms = normalize_rule_terms(trigger_terms or [clean_rule])
        if not terms:
            raise ValueError("at least one trigger term is required")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT rule_text FROM placement_rules WHERE id = ? AND active = 1",
                (rule_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                """
                UPDATE placement_rules
                SET rule_text = ?,
                    trigger_terms = ?,
                    target_location_id = ?,
                    source_command = ?,
                    confidence = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    clean_rule,
                    ",".join(terms),
                    clean_target,
                    source_command.strip(),
                    clamp_confidence(confidence),
                    rule_id,
                ),
            )
            conn.execute(
                "INSERT INTO events(event_type, bin_id, note) VALUES ('rule_updated', ?, ?)",
                (clean_target, f"{rule_id}:{row['rule_text']} -> {clean_rule}"),
            )
        return True

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
        clean_rule = rule_text.strip()
        clean_size = normalize_size_class(size_class)
        if not clean_rule:
            raise ValueError("rule text is required")
        if not clean_size:
            raise ValueError("valid size class is required")
        terms = normalize_rule_terms(trigger_terms or [clean_rule])
        if not terms:
            raise ValueError("at least one trigger term is required")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT rule_text FROM size_rules WHERE id = ? AND active = 1",
                (rule_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                """
                UPDATE size_rules
                SET rule_text = ?,
                    trigger_terms = ?,
                    size_class = ?,
                    source_command = ?,
                    confidence = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    clean_rule,
                    ",".join(terms),
                    clean_size,
                    source_command.strip(),
                    clamp_confidence(confidence),
                    rule_id,
                ),
            )
            conn.execute(
                "INSERT INTO events(event_type, note) VALUES ('size_rule_updated', ?)",
                (f"{rule_id}:{row['rule_text']} -> {clean_rule}",),
            )
        return True

    def deactivate_rule(self, *, rule_id: int, rule_type: str) -> bool:
        table = {
            "placement": "placement_rules",
            "size": "size_rules",
        }.get(rule_type)
        if table is None:
            raise ValueError("rule_type must be placement or size")
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT rule_text FROM {table} WHERE id = ? AND active = 1",
                (rule_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                f"""
                UPDATE {table}
                SET active = 0, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (rule_id,),
            )
            conn.execute(
                "INSERT INTO events(event_type, note) VALUES ('rule_forgotten', ?)",
                (f"{rule_type}:{rule_id}:{row['rule_text']}",),
            )
        return True

    def set_rule_active(
        self, *, rule_id: int, rule_type: str, active: bool
    ) -> bool:
        table = {
            "placement": "placement_rules",
            "size": "size_rules",
        }.get(rule_type)
        if table is None:
            raise ValueError("rule_type must be placement or size")
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT rule_text, active FROM {table} WHERE id = ?",
                (rule_id,),
            ).fetchone()
            if row is None:
                return False
            if bool(row["active"]) == active:
                return True
            conn.execute(
                f"""
                UPDATE {table}
                SET active = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (1 if active else 0, rule_id),
            )
            conn.execute(
                "INSERT INTO events(event_type, note) VALUES (?, ?)",
                (
                    "rule_restored" if active else "rule_forgotten",
                    f"{rule_type}:{rule_id}:{row['rule_text']}",
                ),
            )
        return True

    def recent_examples(self, *, limit: int = 5) -> list[dict[str, object]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT command, action, kind, review_status FROM examples "
                "WHERE review_status != 'rejected' "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_examples(self, query: str, *, limit: int = 3) -> list[dict[str, object]]:
        """Crude similarity for few-shot: examples sharing a word with the query,
        most recent first. Corrections rank ahead of plain confirmations."""
        terms = [t for t in query.lower().split() if len(t) > 2]
        if not terms:
            return self.recent_examples(limit=limit)
        clause = " OR ".join("lower(command) LIKE ?" for _ in terms)
        params: list[object] = [f"%{term}%" for term in terms]
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT command, action, kind, review_status FROM examples
                WHERE review_status != 'rejected' AND ({clause})
                ORDER BY
                    CASE kind
                        WHEN 'correction' THEN 4
                        WHEN 'review_correction' THEN 4
                        WHEN 'edited_photo_sort' THEN 3
                        WHEN 'review_approved' THEN 3
                        WHEN 'rejected_photo_sort' THEN 2
                        ELSE 1
                    END DESC,
                    id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def examples_for_eval(
        self,
        *,
        limit: int = 100,
        kind: str | None = None,
        review_status: str | None = None,
        include_rejected: bool = False,
    ) -> list[dict[str, object]]:
        limit = max(1, min(int(limit), 500))
        clauses: list[str] = []
        params: list[object] = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if review_status:
            clauses.append("review_status = ?")
            params.append(review_status)
        elif not include_rejected:
            clauses.append("review_status != 'rejected'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, command, action, context_json, kind, review_status, reviewed_at, created_at
                FROM examples
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def example_review_counts(self) -> dict[str, int]:
        counts = {"pending": 0, "approved": 0, "promoted": 0, "rejected": 0}
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT review_status, COUNT(*) AS count
                FROM examples
                GROUP BY review_status
                """
            ).fetchall()
        for row in rows:
            status = str(row["review_status"] or "pending")
            counts[status] = int(row["count"])
        counts["active"] = counts["pending"] + counts["approved"] + counts["promoted"]
        counts["total"] = counts["active"] + counts["rejected"]
        return counts

    def example_kind_counts(self, *, include_rejected: bool = False) -> dict[str, int]:
        clauses: list[str] = []
        if not include_rejected:
            clauses.append("review_status != 'rejected'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT kind, COUNT(*) AS count
                FROM examples
                {where}
                GROUP BY kind
                ORDER BY count DESC, kind ASC
                """
            ).fetchall()
        return {str(row["kind"] or "example"): int(row["count"]) for row in rows}

    def add_photo(
        self,
        *,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> PhotoRecord:
        clean_filename = Path(filename).name or "photo"
        suffix = Path(clean_filename).suffix.lower()
        content_type = content_type.strip() or "application/octet-stream"
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}:
            suffix = suffix_for_content_type(content_type)
        photo_dir = self.path.parent / "photos"
        photo_dir.mkdir(parents=True, exist_ok=True)
        target = photo_dir / f"{uuid4().hex}{suffix}"
        target.write_bytes(data)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO photos(filename, content_type, path)
                VALUES (?, ?, ?)
                """,
                (clean_filename, content_type, str(target)),
            )
            photo_id = int(cursor.lastrowid)
        return PhotoRecord(
            id=photo_id,
            filename=clean_filename,
            content_type=content_type,
            path=str(target),
            url=photo_url(photo_id),
        )

    def attach_photo(self, *, item_id: int, photo_id: int) -> None:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE photos SET item_id = ? WHERE id = ?", (item_id, photo_id)
            )
            if cursor.rowcount != 1:
                raise ValueError(f"photo {photo_id} not found")

    def attach_photo_to_location(self, *, location_id: str, photo_id: int) -> None:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE photos SET location_id = ? WHERE id = ?", (location_id, photo_id)
            )
            if cursor.rowcount != 1:
                raise ValueError(f"photo {photo_id} not found")

    def set_photo_target(
        self,
        *,
        photo_id: int,
        item_id: int | None = None,
        location_id: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE photos SET item_id = ?, location_id = ? WHERE id = ?",
                (item_id, location_id, photo_id),
            )

    def location_photos(self, location_id: str) -> list[PhotoRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, item_id, location_id, filename, content_type, path
                FROM photos
                WHERE location_id = ?
                ORDER BY id
                """,
                (location_id,),
            ).fetchall()
        return [photo_from_row(row) for row in rows]

    def location_photos_by_location(
        self, location_ids: list[str]
    ) -> dict[str, list[PhotoRecord]]:
        wanted = {str(location_id) for location_id in location_ids if str(location_id)}
        if not wanted:
            return {}
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, item_id, location_id, filename, content_type, path
                FROM photos
                WHERE location_id IS NOT NULL
                ORDER BY id
                """
            ).fetchall()
        grouped: dict[str, list[PhotoRecord]] = {}
        for row in rows:
            location_id = str(row["location_id"])
            if location_id in wanted:
                grouped.setdefault(location_id, []).append(photo_from_row(row))
        return grouped

    def delete_photo(self, photo_id: int) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT path FROM photos WHERE id = ?", (photo_id,)
            ).fetchone()
            conn.execute(
                "DELETE FROM inventory_review_photos WHERE photo_id = ?",
                (photo_id,),
            )
            conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
        if row is not None:
            try:
                Path(str(row["path"])).unlink()
            except OSError:
                pass

    def photo(self, photo_id: int) -> PhotoRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, item_id, location_id, filename, content_type, path
                FROM photos
                WHERE id = ?
                """,
                (photo_id,),
            ).fetchone()
        if row is None:
            return None
        return photo_from_row(row)

    def photo_content(self, photo_id: int) -> bytes:
        photo = self.photo(photo_id)
        if photo is None:
            raise FileNotFoundError(photo_id)
        return Path(photo.path).read_bytes()


def result_from_row(row: sqlite3.Row) -> SearchResult:
    keys = row.keys()
    aliases = []
    if row["aliases"]:
        aliases = [alias for alias in str(row["aliases"]).split(",") if alias]
    photo_ids = []
    for column in ("photo_ids", "review_photo_ids"):
        if column not in keys or not row[column]:
            continue
        photo_ids.extend(int(photo_id) for photo_id in str(row[column]).split(","))
    photos = [photo_url(photo_id) for photo_id in dict.fromkeys(photo_ids)]
    return SearchResult(
        item_id=int(row["id"]),
        name=str(row["name"]),
        description=str(row["description"]),
        category=str(row["category"]),
        aliases=aliases,
        bin_id=str(row["bin_id"]),
        quantity=float(row["quantity"]),
        unit=str(row["unit"]),
        needs_review=bool(row["needs_review"]) if "needs_review" in keys else False,
        review_reason=str(row["review_reason"] or "") if "review_reason" in keys else "",
        photos=photos,
        size_class=str(row["size_class"] or "") if "size_class" in keys else "",
        size_source=str(row["size_source"] or "") if "size_source" in keys else "",
        size_confidence=float(row["size_confidence"] or 0)
        if "size_confidence" in keys
        else 0.0,
        dimensions=parse_dimensions(row["dimensions_json"])
        if "dimensions_json" in keys
        else None,
        home_location_id=str(row["home_location_id"] or "")
        if "home_location_id" in keys
        else "",
        last_seen_location_id=str(row["last_seen_location_id"] or "")
        if "last_seen_location_id" in keys
        else "",
        active=bool(row["active"]) if "active" in keys else True,
        archived_at=(
            str(row["archived_at"])
            if "archived_at" in keys and row["archived_at"]
            else None
        ),
    )


def normalize_rule_terms(values: list[str]) -> list[str]:
    terms: set[str] = set()
    for value in values:
        for term in re.findall(r"[a-z0-9]+", str(value).lower()):
            if len(term) > 2 and term not in RULE_STOP_WORDS:
                terms.add(term)
    return sorted(terms)


SIZE_CLASSES = {"tiny", "small", "medium", "large", "oversize"}


def normalize_size_class(value: str | None) -> str:
    clean = (value or "").strip().lower()
    aliases = {
        "xs": "tiny",
        "extra-small": "tiny",
        "extra small": "tiny",
        "med": "medium",
        "big": "large",
        "xl": "oversize",
        "extra-large": "oversize",
        "extra large": "oversize",
    }
    clean = aliases.get(clean, clean)
    return clean if clean in SIZE_CLASSES else ""


def clamp_confidence(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


RULE_STOP_WORDS = {
    "always",
    "belong",
    "belongs",
    "keep",
    "kept",
    "put",
    "store",
    "stored",
    "thing",
    "things",
    "this",
    "that",
    "the",
    "and",
    "with",
    "for",
    "into",
    "inside",
    "main",
    "cabinet",
    "drawer",
    "shelf",
}


def rule_from_row(row: sqlite3.Row) -> LearnedRule:
    terms = [
        term.strip()
        for term in str(row["trigger_terms"] or "").split(",")
        if term.strip()
    ]
    return LearnedRule(
        id=int(row["id"]),
        rule_text=str(row["rule_text"]),
        trigger_terms=terms,
        target_location_id=str(row["target_location_id"]),
        source_command=str(row["source_command"] or ""),
        confidence=float(row["confidence"]),
        active=bool(row["active"]),
    )


def size_rule_from_row(row: sqlite3.Row) -> LearnedSizeRule:
    terms = [
        term.strip()
        for term in str(row["trigger_terms"] or "").split(",")
        if term.strip()
    ]
    return LearnedSizeRule(
        id=int(row["id"]),
        rule_text=str(row["rule_text"]),
        trigger_terms=terms,
        size_class=str(row["size_class"]),
        source_command=str(row["source_command"] or ""),
        confidence=float(row["confidence"]),
        active=bool(row["active"]),
    )


def _placement_rule_conflicts(
    rules: list[LearnedRule], *, limit: int
) -> list[dict[str, object]]:
    conflicts: list[dict[str, object]] = []
    seen: set[tuple[int, int]] = set()
    for index, left in enumerate(rules):
        left_terms = set(left.trigger_terms)
        if not left_terms:
            continue
        for right in rules[index + 1 :]:
            key = tuple(sorted((left.id, right.id)))
            if key in seen or left.target_location_id == right.target_location_id:
                continue
            shared_terms = sorted(left_terms & set(right.trigger_terms))
            if not shared_terms:
                continue
            seen.add(key)
            conflicts.append(
                {
                    "rule_type": "placement",
                    "rule_ids": [left.id, right.id],
                    "shared_terms": shared_terms,
                    "targets": [left.target_location_id, right.target_location_id],
                    "rules": [left.as_dict(), right.as_dict()],
                }
            )
            if len(conflicts) >= limit:
                return conflicts
    return conflicts


def _size_rule_conflicts(
    rules: list[LearnedSizeRule], *, limit: int
) -> list[dict[str, object]]:
    conflicts: list[dict[str, object]] = []
    seen: set[tuple[int, int]] = set()
    for index, left in enumerate(rules):
        left_terms = set(left.trigger_terms)
        if not left_terms:
            continue
        for right in rules[index + 1 :]:
            key = tuple(sorted((left.id, right.id)))
            if key in seen or left.size_class == right.size_class:
                continue
            shared_terms = sorted(left_terms & set(right.trigger_terms))
            if not shared_terms:
                continue
            seen.add(key)
            conflicts.append(
                {
                    "rule_type": "size",
                    "rule_ids": [left.id, right.id],
                    "shared_terms": shared_terms,
                    "size_classes": [left.size_class, right.size_class],
                    "rules": [left.as_dict(), right.as_dict()],
                }
            )
            if len(conflicts) >= limit:
                return conflicts
    return conflicts


def parse_dimensions(raw: object) -> dict[str, object] | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def photo_from_row(row: sqlite3.Row) -> PhotoRecord:
    photo_id = int(row["id"])
    return PhotoRecord(
        id=photo_id,
        filename=str(row["filename"]),
        content_type=str(row["content_type"]),
        path=str(row["path"]),
        url=photo_url(photo_id),
        item_id=int(row["item_id"]) if "item_id" in row.keys() and row["item_id"] is not None else None,
        location_id=str(row["location_id"]) if "location_id" in row.keys() and row["location_id"] is not None else None,
    )


def _natural_text_key(value: object) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", str(value or ""))
        if part
    )


def _natural_compare(left: str, right: str) -> int:
    left_key = _natural_text_key(left)
    right_key = _natural_text_key(right)
    return (left_key > right_key) - (left_key < right_key)


def inventory_catalog_page_from_results(
    rows: list[SearchResult],
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
    clean_query = " ".join(query.split()).casefold()[:200]
    clean_category = category.strip().casefold()[:100]
    clean_location_ids = {
        str(location_id).strip()
        for location_id in location_ids or set()
        if str(location_id).strip()
    }
    clean_limit = max(1, min(int(limit), 100))
    clean_offset = max(0, int(offset))
    active_rows = [row for row in rows if row.active != archived]
    metric_rows = (
        [row for row in active_rows if row.bin_id in clean_location_ids]
        if clean_location_ids
        else active_rows
    )

    grouped: dict[int, list[SearchResult]] = {}
    for row in active_rows:
        grouped.setdefault(row.item_id, []).append(row)
    metric_grouped: dict[int, list[SearchResult]] = {}
    for row in metric_rows:
        metric_grouped.setdefault(row.item_id, []).append(row)

    def matches(placements: list[SearchResult]) -> bool:
        first = placements[0]
        if clean_category and first.category.strip().casefold() != clean_category:
            return False
        if clean_location_ids and not any(
            row.bin_id in clean_location_ids for row in placements
        ):
            return False
        if clean_scope == "review" and not any(row.needs_review for row in placements):
            return False
        if clean_scope == "photos" and not any(row.photos for row in placements):
            return False
        if clean_scope == "no-photo" and any(row.photos for row in placements):
            return False
        if clean_scope == "uncategorized" and first.category.strip():
            return False
        if clean_query:
            haystack = " ".join(
                [
                    first.name,
                    first.category,
                    first.description,
                    *first.aliases,
                    *(row.bin_id for row in placements),
                ]
            ).casefold()
            if clean_query not in haystack:
                return False
        return True

    groups = [placements for placements in grouped.values() if matches(placements)]
    if clean_sort == "category":
        groups.sort(
            key=lambda placements: (
                _natural_text_key(placements[0].category or "Uncategorized"),
                _natural_text_key(placements[0].name),
            )
        )
    elif clean_sort == "location":
        groups.sort(
            key=lambda placements: (
                min(_natural_text_key(row.bin_id) for row in placements),
                _natural_text_key(placements[0].name),
            )
        )
    elif clean_sort == "quantity":
        groups.sort(
            key=lambda placements: (
                -sum(row.quantity for row in placements),
                _natural_text_key(placements[0].name),
            )
        )
    else:
        groups.sort(key=lambda placements: _natural_text_key(placements[0].name))

    page = groups[clean_offset : clean_offset + clean_limit]
    return InventoryCatalogPage(
        items=[row for placements in page for row in placements],
        total=len(groups),
        metrics={
            "items": len(metric_grouped),
            "placements": len(metric_rows),
            "locations": len({row.bin_id for row in metric_rows}),
            "need_review": sum(
                1
                for placements in metric_grouped.values()
                if any(row.needs_review for row in placements)
            ),
        },
        categories=sorted(
            {
                placements[0].category.strip()
                for placements in metric_grouped.values()
                if placements[0].category.strip()
            },
            key=str.casefold,
        ),
    )


def photo_url(photo_id: int) -> str:
    return f"/photos/{photo_id}"


def suffix_for_content_type(content_type: str) -> str:
    clean = content_type.split(";", 1)[0].strip().lower()
    if clean in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    if clean == "image/png":
        return ".png"
    if clean == "image/webp":
        return ".webp"
    if clean == "image/gif":
        return ".gif"
    if clean in {"image/heic", "image/heif"}:
        return ".heic"
    return ".bin"


def ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
