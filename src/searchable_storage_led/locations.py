"""Location tree: the generalized storage model.

Slice 1 of the vision (see docs/vision.md). This adds a `locations` table that
represents every storage spot as a node in a tree with an *optional* light
binding. Existing cabinet bins are backfilled as children of a `main-cabinet`
root, carrying their current LED indexes, so the cabinet is represented
identically to `layout.py`.

The live find and put-away paths use this graph. Item destinations can be lit
directly or resolve through `nearest_lit` to an edge-controlled ancestor while
the full path remains available for spoken and visual directions.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path

from .layout import StorageLayout
from .words import label_for_index


LOCATION_TYPES = (
    "home",
    "room",
    "area",
    "closet",
    "cabinet",
    "shelf",
    "drawer",
    "tray",
    "bag",
    "organizer",
    "tote",
    "box",
    "bin",
    "pouch",
    "rack",
    "locker",
    "storage_unit",
    "cell",
)

ITEM_DESTINATION_TYPES = {
    "shelf",
    "drawer",
    "tray",
    "bag",
    "organizer",
    "tote",
    "box",
    "bin",
    "pouch",
    "locker",
    "cell",
}


class LocationNameConflictError(ValueError):
    """An active sibling already uses the requested normalized label."""


def _normalized_location_label(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def default_accepts_items(location_type: str | None) -> bool:
    return str(location_type or "").strip().lower() in ITEM_DESTINATION_TYPES


def normalize_location_type(value: object) -> str:
    location_type = str(value or "").strip().lower()
    location_type = {
        "shelf-unit": "storage_unit",
        "storage unit": "storage_unit",
    }.get(location_type, location_type)
    if location_type not in LOCATION_TYPES:
        raise ValueError("unknown storage place type")
    return location_type


@dataclass(frozen=True)
class Location:
    id: str
    type: str
    parent_id: str | None = None
    label: str = ""
    grid: dict | None = None  # this node's own coordinate space, e.g. {"cols": 7, "rows": 9}
    col: int | None = None  # position of this node within its parent's grid
    row: int | None = None
    w: int = 1  # footprint within the parent's grid (a wide bin / a big drawer)
    h: int = 1
    light_target: str | None = None  # controller id; None = unlit
    light_indexes: list[int] = field(default_factory=list)  # empty = unlit
    capacity_class: str = "medium"  # tiny | small | medium | large | oversize
    purpose_tags: list[str] = field(default_factory=list)
    placement_notes: str = ""
    internal_dimensions: dict[str, float] = field(default_factory=dict)
    accepts_items: bool = True
    active: bool = True
    archived_at: str | None = None
    archive_group: str | None = None

    @property
    def is_lit(self) -> bool:
        return bool(self.light_indexes)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "type": self.type,
            "parent_id": self.parent_id,
            "label": self.label,
            "grid": self.grid,
            "col": self.col,
            "row": self.row,
            "w": self.w,
            "h": self.h,
            "light_target": self.light_target,
            "light_indexes": list(self.light_indexes),
            "capacity_class": self.capacity_class,
            "purpose_tags": list(self.purpose_tags),
            "placement_notes": self.placement_notes,
            "internal_dimensions": dict(self.internal_dimensions),
            "accepts_items": self.accepts_items,
            "active": self.active,
            "archived_at": self.archived_at,
            "archive_group": self.archive_group,
        }


def location_path(locations, location_id: str) -> list[Location]:
    """Return a location's root-to-node path for any repository backend."""
    path: list[Location] = []
    seen: set[str] = set()
    current = locations.get_any(location_id)
    while current is not None and current.id not in seen:
        path.append(current)
        seen.add(current.id)
        current = locations.get_any(current.parent_id) if current.parent_id else None
    return list(reversed(path))


def match_locations(
    reference: object,
    locations,
    *,
    active: bool | None = True,
) -> list[Location]:
    """Match an id, label, or qualified path without guessing between duplicates."""
    raw = re.sub(r"\s+", " ", str(reference or "")).strip(" .?!")
    if not raw:
        return []
    rows = [
        location
        for location in locations.all(include_archived=True)
        if active is None or location.active is active
    ]

    def normalized(value: object) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().casefold()

    exact_ids = [location for location in rows if normalized(location.id) == normalized(raw)]
    if exact_ids:
        return exact_ids

    parts: list[str] = []
    if re.search(r"\s*(?:>|/)\s*", raw):
        parts = [part for part in re.split(r"\s*(?:>|/)\s*", raw) if part]
    else:
        natural = re.fullmatch(
            r"(?P<child>.+?)\s+(?:in|inside|under|within)\s+(?P<parent>.+)",
            raw,
            re.I,
        )
        if natural:
            parts = [natural.group("parent"), natural.group("child")]

    if parts:
        wanted = [normalized(part) for part in parts]
        matches: list[Location] = []
        for location in rows:
            path = location_path(locations, location.id)
            if len(path) < len(wanted):
                continue
            tail = path[-len(wanted) :]
            if all(
                expected in {normalized(node.id), normalized(node.label)}
                for expected, node in zip(wanted, tail, strict=True)
            ):
                matches.append(location)
        return matches

    lowered = normalized(raw)
    label_matches = [location for location in rows if normalized(location.label) == lowered]
    if label_matches:
        return label_matches
    slug = re.sub(r"[^a-z0-9]+", "-", raw.casefold()).strip("-")
    return [location for location in rows if location.id.casefold() == slug]


@dataclass(frozen=True)
class HardwareBinding:
    id: int
    location_id: str
    kind: str
    controller_id: str = ""
    address: str = ""
    metadata: dict = field(default_factory=dict)
    active: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "location_id": self.location_id,
            "kind": self.kind,
            "controller_id": self.controller_id,
            "address": self.address,
            "metadata": dict(self.metadata),
            "active": self.active,
        }


class LocationStore:
    """The `locations` table. Shares the inventory DB file but owns its table."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def health_probe(self) -> None:
        with self.connect() as conn:
            conn.execute("SELECT id FROM locations LIMIT 1").fetchone()

    def invalidate_cache(self) -> None:
        """Keep the shared repository contract; SQLite reads are already current."""

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS locations (
                    id            TEXT PRIMARY KEY,
                    parent_id     TEXT REFERENCES locations(id),
                    type          TEXT NOT NULL,
                    label         TEXT NOT NULL DEFAULT '',
                    grid_json     TEXT,
                    cell_col      INTEGER,
                    cell_row      INTEGER,
                    cell_w        INTEGER DEFAULT 1,
                    cell_h        INTEGER DEFAULT 1,
                    light_target  TEXT,
                    light_indexes TEXT,
                    capacity_class TEXT NOT NULL DEFAULT 'medium',
                    purpose_tags  TEXT,
                    placement_notes TEXT NOT NULL DEFAULT '',
                    internal_dimensions_json TEXT,
                    accepts_items INTEGER NOT NULL DEFAULT 1,
                    active INTEGER NOT NULL DEFAULT 1,
                    archived_at TEXT,
                    archive_group TEXT,
                    content_revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_locations_parent
                    ON locations(parent_id);

                CREATE TABLE IF NOT EXISTS hardware_bindings (
                    id INTEGER PRIMARY KEY,
                    location_id TEXT NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    controller_id TEXT NOT NULL DEFAULT '',
                    address TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(location_id, kind, controller_id, address)
                );
                CREATE INDEX IF NOT EXISTS idx_hardware_bindings_location
                    ON hardware_bindings(location_id);

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    item_id INTEGER,
                    bin_id TEXT,
                    quantity REAL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_events_location_type
                    ON events(bin_id, event_type, id DESC);
                """
            )
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(locations)")}
            for name, ddl in (
                ("cell_col", "INTEGER"),
                ("cell_row", "INTEGER"),
                ("cell_w", "INTEGER DEFAULT 1"),
                ("cell_h", "INTEGER DEFAULT 1"),
                ("capacity_class", "TEXT NOT NULL DEFAULT 'medium'"),
                ("purpose_tags", "TEXT"),
                ("placement_notes", "TEXT NOT NULL DEFAULT ''"),
                ("internal_dimensions_json", "TEXT"),
                ("accepts_items", "INTEGER NOT NULL DEFAULT 1"),
                ("active", "INTEGER NOT NULL DEFAULT 1"),
                ("archived_at", "TEXT"),
                ("archive_group", "TEXT"),
                ("content_revision", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in existing:
                    conn.execute(f"ALTER TABLE locations ADD COLUMN {name} {ddl}")
            conn.execute(
                "UPDATE locations SET archive_group = id "
                "WHERE active = 0 AND archive_group IS NULL"
            )
            conn.execute(
                "UPDATE locations SET archive_group = NULL "
                "WHERE active = 1 AND archive_group IS NOT NULL"
            )
            tables = {
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if {"inventory", "items"}.issubset(tables):
                conn.executescript(
                    """
                CREATE TRIGGER IF NOT EXISTS inventory_content_revision_insert
                AFTER INSERT ON inventory
                BEGIN
                    UPDATE locations
                    SET content_revision = content_revision + 1
                    WHERE id IN (
                        WITH RECURSIVE affected(id, parent_id) AS (
                            SELECT id, parent_id FROM locations WHERE id = NEW.bin_id
                            UNION ALL
                            SELECT parent.id, parent.parent_id
                            FROM locations parent
                            JOIN affected child ON parent.id = child.parent_id
                        )
                        SELECT id FROM affected
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS inventory_content_revision_delete
                AFTER DELETE ON inventory
                BEGIN
                    UPDATE locations
                    SET content_revision = content_revision + 1
                    WHERE id IN (
                        WITH RECURSIVE affected(id, parent_id) AS (
                            SELECT id, parent_id FROM locations WHERE id = OLD.bin_id
                            UNION ALL
                            SELECT parent.id, parent.parent_id
                            FROM locations parent
                            JOIN affected child ON parent.id = child.parent_id
                        )
                        SELECT id FROM affected
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS inventory_content_revision_update
                AFTER UPDATE OF item_id, bin_id, quantity, unit ON inventory
                WHEN OLD.item_id IS NOT NEW.item_id
                  OR OLD.bin_id IS NOT NEW.bin_id
                  OR OLD.quantity IS NOT NEW.quantity
                  OR OLD.unit IS NOT NEW.unit
                BEGIN
                    UPDATE locations
                    SET content_revision = content_revision + 1
                    WHERE id IN (
                        WITH RECURSIVE affected(id, parent_id) AS (
                            SELECT id, parent_id FROM locations
                            WHERE id IN (OLD.bin_id, NEW.bin_id)
                            UNION
                            SELECT parent.id, parent.parent_id
                            FROM locations parent
                            JOIN affected child ON parent.id = child.parent_id
                        )
                        SELECT id FROM affected
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS item_active_content_revision_update
                AFTER UPDATE OF active ON items
                WHEN OLD.active IS NOT NEW.active
                BEGIN
                    UPDATE locations
                    SET content_revision = content_revision + 1
                    WHERE id IN (
                        WITH RECURSIVE affected(id, parent_id) AS (
                            SELECT location.id, location.parent_id
                            FROM locations location
                            JOIN inventory entry ON entry.bin_id = location.id
                            WHERE entry.item_id = NEW.id
                            UNION
                            SELECT parent.id, parent.parent_id
                            FROM locations parent
                            JOIN affected child ON parent.id = child.parent_id
                        )
                        SELECT id FROM affected
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS location_structure_revision_insert
                AFTER INSERT ON locations
                WHEN NEW.parent_id IS NOT NULL
                BEGIN
                    UPDATE locations
                    SET content_revision = content_revision + 1
                    WHERE id IN (
                        WITH RECURSIVE affected(id, parent_id) AS (
                            SELECT id, parent_id FROM locations WHERE id = NEW.parent_id
                            UNION ALL
                            SELECT parent.id, parent.parent_id
                            FROM locations parent
                            JOIN affected child ON parent.id = child.parent_id
                        )
                        SELECT id FROM affected
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS location_structure_revision_update
                AFTER UPDATE OF parent_id, active ON locations
                WHEN OLD.parent_id IS NOT NEW.parent_id OR OLD.active IS NOT NEW.active
                BEGIN
                    UPDATE locations
                    SET content_revision = content_revision + 1
                    WHERE id IN (
                        WITH RECURSIVE affected(id, parent_id) AS (
                            SELECT id, parent_id FROM locations
                            WHERE id IN (NEW.id, OLD.parent_id, NEW.parent_id)
                            UNION
                            SELECT parent.id, parent.parent_id
                            FROM locations parent
                            JOIN affected child ON parent.id = child.parent_id
                        )
                        SELECT id FROM affected
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS location_structure_revision_delete
                AFTER DELETE ON locations
                WHEN OLD.parent_id IS NOT NULL
                BEGIN
                    UPDATE locations
                    SET content_revision = content_revision + 1
                    WHERE id IN (
                        WITH RECURSIVE affected(id, parent_id) AS (
                            SELECT id, parent_id FROM locations WHERE id = OLD.parent_id
                            UNION ALL
                            SELECT parent.id, parent.parent_id
                            FROM locations parent
                            JOIN affected child ON parent.id = child.parent_id
                        )
                        SELECT id FROM affected
                    );
                END;
                    """
                )

    def upsert(self, loc: Location) -> None:
        with self.connect() as conn:
            # Serialize the sibling check with the write. API preflight improves
            # error messages, but every repository caller must preserve the graph.
            conn.execute("BEGIN IMMEDIATE")
            self._upsert_with_connection(conn, loc)

    def create_many(self, rows: list[Location]) -> list[str]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self.create_many_with_connection(conn, rows)

    def create_many_with_connection(
        self,
        conn: sqlite3.Connection,
        rows: list[Location],
    ) -> list[str]:
        """Create a parent-ordered structure inside the caller's transaction."""
        if not rows:
            return []
        ids = [location.id for location in rows]
        if len(ids) != len(set(ids)):
            raise ValueError("location IDs in one structure must be unique")
        placeholders = ",".join("?" for _ in ids)
        existing = conn.execute(
            f"SELECT id FROM locations WHERE id IN ({placeholders}) LIMIT 1",
            ids,
        ).fetchone()
        if existing is not None:
            raise ValueError(f"location already exists: {existing['id']}")
        for location in rows:
            self._upsert_with_connection(conn, location)
        return ids

    def _upsert_with_connection(
        self, conn: sqlite3.Connection, loc: Location
    ) -> None:
        if loc.parent_id == loc.id:
            raise ValueError("location cannot be its own parent")
        if loc.parent_id is not None:
            parent = conn.execute(
                "SELECT id, parent_id, active FROM locations WHERE id = ?",
                (loc.parent_id,),
            ).fetchone()
            if parent is None:
                raise KeyError(f"unknown parent: {loc.parent_id}")
            if loc.active and not bool(parent["active"]):
                raise ValueError(f"parent location is archived: {loc.parent_id}")
            seen: set[str] = set()
            current = parent
            while current is not None and current["id"] not in seen:
                if current["id"] == loc.id:
                    raise ValueError("location cannot move inside its own subtree")
                seen.add(current["id"])
                current = (
                    conn.execute(
                        "SELECT id, parent_id, active FROM locations WHERE id = ?",
                        (current["parent_id"],),
                    ).fetchone()
                    if current["parent_id"] is not None
                    else None
                )
        existing = conn.execute(
            "SELECT parent_id, label, active FROM locations WHERE id = ?",
            (loc.id,),
        ).fetchone()
        unchanged_active_identity = bool(
            existing is not None
            and bool(existing["active"])
            and loc.active
            and existing["parent_id"] == loc.parent_id
            and _normalized_location_label(existing["label"])
            == _normalized_location_label(loc.label or loc.id)
        )
        if loc.active and not unchanged_active_identity:
            siblings = conn.execute(
                """
                SELECT id, label
                FROM locations
                WHERE parent_id IS ? AND active = 1 AND id != ?
                """,
                (loc.parent_id, loc.id),
            ).fetchall()
            normalized = _normalized_location_label(loc.label or loc.id)
            if any(
                _normalized_location_label(row["label"] or row["id"])
                == normalized
                for row in siblings
            ):
                raise LocationNameConflictError(
                    "A storage place with that name already exists at this level."
                )
        conn.execute(
            """
            INSERT INTO locations(
                id, parent_id, type, label, grid_json,
                cell_col, cell_row, cell_w, cell_h, light_target, light_indexes,
                capacity_class, purpose_tags, placement_notes,
                internal_dimensions_json, accepts_items, active, archived_at,
                archive_group
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                parent_id     = excluded.parent_id,
                type          = excluded.type,
                label         = excluded.label,
                grid_json     = excluded.grid_json,
                cell_col      = excluded.cell_col,
                cell_row      = excluded.cell_row,
                cell_w        = excluded.cell_w,
                cell_h        = excluded.cell_h,
                light_target  = excluded.light_target,
                light_indexes = excluded.light_indexes,
                capacity_class = excluded.capacity_class,
                purpose_tags   = excluded.purpose_tags,
                placement_notes = excluded.placement_notes,
                internal_dimensions_json = excluded.internal_dimensions_json,
                accepts_items = excluded.accepts_items,
                active        = excluded.active,
                archived_at   = excluded.archived_at,
                archive_group = excluded.archive_group
            """,
            (
                loc.id,
                loc.parent_id,
                loc.type,
                loc.label,
                json.dumps(loc.grid) if loc.grid is not None else None,
                loc.col,
                loc.row,
                loc.w,
                loc.h,
                loc.light_target,
                json.dumps(list(loc.light_indexes)) if loc.light_indexes else None,
                normalize_capacity_class(loc.capacity_class),
                json.dumps([tag for tag in loc.purpose_tags if tag.strip()])
                if loc.purpose_tags
                else None,
                loc.placement_notes.strip(),
                json.dumps(
                    normalize_internal_dimensions(loc.internal_dimensions),
                    sort_keys=True,
                )
                if loc.internal_dimensions
                else None,
                1 if loc.accepts_items else 0,
                1 if loc.active else 0,
                loc.archived_at,
                None if loc.active else (loc.archive_group or loc.id),
            ),
        )
        if loc.light_target and loc.light_indexes:
            _upsert_hardware_binding(
                conn,
                location_id=loc.id,
                kind="wled_pixels",
                controller_id=loc.light_target,
                address=",".join(str(index) for index in loc.light_indexes),
                metadata={"pixels": list(loc.light_indexes)},
                active=True,
            )

    def get(self, location_id: str, *, include_archived: bool = False) -> Location | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM locations WHERE id = ? AND (? OR active = 1)",
                (location_id, 1 if include_archived else 0),
            ).fetchone()
        return location_from_row(row) if row is not None else None

    def get_any(self, location_id: str) -> Location | None:
        return self.get(location_id, include_archived=True)

    def children(
        self, parent_id: str | None, *, include_archived: bool = False
    ) -> list[Location]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM locations WHERE parent_id IS ? AND (? OR active = 1) ORDER BY id",
                (parent_id, 1 if include_archived else 0),
            ).fetchall()
        return [location_from_row(row) for row in rows]

    def all(self, *, include_archived: bool = False) -> list[Location]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM locations WHERE ? OR active = 1 ORDER BY id",
                (1 if include_archived else 0,),
            ).fetchall()
        return [location_from_row(row) for row in rows]

    def count(self, *, include_archived: bool = False) -> int:
        with self.connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM locations WHERE ? OR active = 1",
                    (1 if include_archived else 0,),
                ).fetchone()[0]
            )

    def ancestors(self, location_id: str) -> list[Location]:
        """The node and its parents up to the root, nearest first."""
        chain: list[Location] = []
        seen: set[str] = set()
        current = self.get(location_id)
        while current is not None and current.id not in seen:
            chain.append(current)
            seen.add(current.id)
            if current.parent_id is None:
                break
            current = self.get(current.parent_id)
        return chain

    def subtree_ids(
        self, location_id: str, *, include_archived: bool = False
    ) -> list[str]:
        """This location plus every location nested inside it (ancestor first)."""
        ids: list[str] = []
        queue = [location_id]
        while queue:
            current = queue.pop()
            ids.append(current)
            queue.extend(
                child.id
                for child in self.children(current, include_archived=include_archived)
            )
        return ids

    def archive_subtree(self, location_id: str) -> list[str]:
        if self.get(location_id) is None:
            raise KeyError(f"unknown active location: {location_id}")
        ids = self.subtree_ids(location_id)
        with self.connect() as conn:
            conn.executemany(
                "UPDATE locations SET active = 0, archived_at = CURRENT_TIMESTAMP, "
                "archive_group = ? WHERE id = ?",
                [(location_id, child_id) for child_id in ids],
            )
        return ids

    def restore_subtree(self, location_id: str) -> list[str]:
        location = self.get_any(location_id)
        if location is None:
            raise KeyError(f"unknown location: {location_id}")
        if location.parent_id:
            parent = self.get_any(location.parent_id)
            if parent is not None and not parent.active:
                raise ValueError("restore the parent location first")
        all_ids = self.subtree_ids(location_id, include_archived=True)
        archive_group = location.archive_group or location.id
        ids = [
            child_id
            for child_id in all_ids
            if (
                (child := self.get_any(child_id)) is not None
                and not child.active
                and (child.archive_group or child.id) == archive_group
            )
        ]
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT id, parent_id, label FROM locations WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
            restoring_ids = {str(row["id"]) for row in rows}
            restoring_names: dict[tuple[str | None, str], str] = {}
            for row in rows:
                key = (
                    row["parent_id"],
                    _normalized_location_label(row["label"] or row["id"]),
                )
                previous = restoring_names.get(key)
                if previous is not None and previous != row["id"]:
                    raise LocationNameConflictError(
                        "A storage place with that name already exists at this level."
                    )
                restoring_names[key] = str(row["id"])
                active_siblings = conn.execute(
                    """
                    SELECT id, label
                    FROM locations
                    WHERE parent_id IS ? AND active = 1
                    """,
                    (row["parent_id"],),
                ).fetchall()
                if any(
                    str(sibling["id"]) not in restoring_ids
                    and _normalized_location_label(sibling["label"] or sibling["id"])
                    == key[1]
                    for sibling in active_siblings
                ):
                    raise LocationNameConflictError(
                        "A storage place with that name already exists at this level."
                    )
            conn.executemany(
                "UPDATE locations SET active = 1, archived_at = NULL, "
                "archive_group = NULL WHERE id = ?",
                [(location_id,) for location_id in ids],
            )
        return ids

    def delete_subtree(self, location_id: str) -> list[str]:
        """Delete a location and everything nested inside it."""
        ids = self.subtree_ids(location_id, include_archived=True)
        # Delete leaves first: a descendant always appears later in `ids` than
        # its ancestor, so reversed order respects the parent_id foreign key.
        with self.connect() as conn:
            conn.executemany(
                "DELETE FROM locations WHERE id = ?", [(i,) for i in reversed(ids)]
            )
        return ids

    def move(self, location_id: str, new_parent_id: str | None) -> Location:
        """Re-parent a location (and everything inside it comes along, since
        items point at this node, not its old parent)."""
        loc = self.get(location_id)
        if loc is None:
            raise KeyError(f"unknown location: {location_id}")
        if new_parent_id is not None and self.get(new_parent_id) is None:
            raise KeyError(f"unknown parent: {new_parent_id}")
        if new_parent_id == location_id:
            raise ValueError("location cannot be its own parent")
        if new_parent_id is not None and new_parent_id in self.subtree_ids(location_id):
            raise ValueError("location cannot move inside its own subtree")
        moved = replace(loc, parent_id=new_parent_id)
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
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO events(event_type, bin_id, quantity, note)
                VALUES (?, ?, ?, ?)
                """,
                (clean_type, location_id, quantity, note.strip()),
            )

    def events(self, location_id: str, *, limit: int = 25) -> list[dict[str, object]]:
        clean_limit = max(1, min(int(limit), 100))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, event_type, item_id, bin_id, quantity, note, created_at
                FROM events
                WHERE bin_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (location_id, clean_limit),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "event_type": str(row["event_type"]),
                "item_id": int(row["item_id"]) if row["item_id"] is not None else None,
                "location_id": str(row["bin_id"] or ""),
                "quantity": float(row["quantity"]) if row["quantity"] is not None else None,
                "note": str(row["note"] or ""),
                "created_at": row["created_at"],
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
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, event_type, item_id, bin_id, quantity, note, created_at
                FROM events
                WHERE bin_id = ? AND event_type = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (location_id, clean_type),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "event_type": str(row["event_type"]),
            "item_id": int(row["item_id"]) if row["item_id"] is not None else None,
            "location_id": str(row["bin_id"] or ""),
            "quantity": float(row["quantity"]) if row["quantity"] is not None else None,
            "note": str(row["note"] or ""),
            "created_at": row["created_at"],
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
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, event_type, item_id, bin_id, quantity, note, created_at
                FROM (
                    SELECT
                        id, event_type, item_id, bin_id, quantity, note, created_at,
                        ROW_NUMBER() OVER (PARTITION BY bin_id ORDER BY id DESC) AS rank
                    FROM events
                    WHERE event_type = ? AND bin_id <> ''
                )
                WHERE rank = 1
                ORDER BY id DESC
                LIMIT ?
                """,
                (clean_type, clean_limit + 1),
            ).fetchall()
        truncated = len(rows) > clean_limit
        events = {
            str(row["bin_id"]): {
                "id": int(row["id"]),
                "event_type": str(row["event_type"]),
                "item_id": (
                    int(row["item_id"]) if row["item_id"] is not None else None
                ),
                "location_id": str(row["bin_id"]),
                "quantity": (
                    float(row["quantity"]) if row["quantity"] is not None else None
                ),
                "note": str(row["note"] or ""),
                "created_at": row["created_at"],
            }
            for row in rows[:clean_limit]
        }
        return events, truncated

    def has_event(self, event_type: str, *, location_id: str = "") -> bool:
        clean_type = event_type.strip()
        if not clean_type:
            return False
        query = "SELECT 1 FROM events WHERE event_type = ?"
        values: list[object] = [clean_type]
        if location_id:
            query += " AND bin_id = ?"
            values.append(location_id)
        query += " LIMIT 1"
        with self.connect() as conn:
            return conn.execute(query, values).fetchone() is not None

    def nearest_lit(self, location_id: str) -> Location | None:
        """Walk up from a node to the closest ancestor that can light up.

        This is the primitive behind graceful degradation: light the nearest
        lit location, then words cover the rest of the path down to the item.
        """
        for loc in self.ancestors(location_id):
            if loc.is_lit:
                return loc
        return None

    def hardware_bindings(self, location_id: str) -> list[HardwareBinding]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, location_id, kind, controller_id, address, metadata_json, active
                FROM hardware_bindings
                WHERE location_id = ?
                ORDER BY active DESC, kind, id
                """,
                (location_id,),
            ).fetchall()
        return [hardware_binding_from_row(row) for row in rows]

    def hardware_bindings_by_location(
        self, location_ids: list[str]
    ) -> dict[str, list[HardwareBinding]]:
        wanted = {str(location_id) for location_id in location_ids if str(location_id)}
        if not wanted:
            return {}
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, location_id, kind, controller_id, address, metadata_json, active
                FROM hardware_bindings
                ORDER BY active DESC, kind, id
                """
            ).fetchall()
        grouped: dict[str, list[HardwareBinding]] = {}
        for row in rows:
            location_id = str(row["location_id"])
            if location_id in wanted:
                grouped.setdefault(location_id, []).append(hardware_binding_from_row(row))
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
        if self.get(location_id) is None:
            raise KeyError(f"unknown location: {location_id}")
        clean_kind = normalize_binding_kind(kind)
        if not clean_kind:
            raise ValueError("unsupported hardware binding kind")
        clean_controller = controller_id.strip()
        clean_address = address.strip()
        data = metadata or {}
        with self.connect() as conn:
            if active:
                conn.execute(
                    """
                    UPDATE hardware_bindings
                    SET active = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE location_id = ? AND kind = ? AND controller_id = ?
                    """,
                    (location_id, clean_kind, clean_controller),
                )
            binding_id = _upsert_hardware_binding(
                conn,
                location_id=location_id,
                kind=clean_kind,
                controller_id=clean_controller,
                address=clean_address,
                metadata=data,
                active=active,
            )
            if clean_kind == "wled_pixels":
                pixels = pixels_from_binding(clean_address, data)
                conn.execute(
                    """
                    UPDATE locations
                    SET light_target = ?, light_indexes = ?
                    WHERE id = ?
                    """,
                    (
                        clean_controller or None,
                        json.dumps(pixels) if pixels and active else None,
                        location_id,
                    ),
                )
            row = conn.execute(
                """
                SELECT id, location_id, kind, controller_id, address, metadata_json, active
                FROM hardware_bindings
                WHERE id = ?
                """,
                (binding_id,),
            ).fetchone()
        return hardware_binding_from_row(row)

    def set_hardware_bindings(
        self, rows: list[dict[str, object]]
    ) -> list[HardwareBinding]:
        if not rows:
            return []
        normalized: list[dict[str, object]] = []
        identities: set[tuple[str, str, str, str]] = set()
        for row in rows:
            location_id = str(row.get("location_id") or "").strip()
            clean_kind = normalize_binding_kind(str(row.get("kind") or ""))
            if not clean_kind:
                raise ValueError("unsupported hardware binding kind")
            clean_controller = str(row.get("controller_id") or "").strip()
            clean_address = str(row.get("address") or "").strip()
            identity = (location_id, clean_kind, clean_controller, clean_address)
            if identity in identities:
                raise ValueError("duplicate hardware binding in one batch")
            identities.add(identity)
            normalized.append(
                {
                    "location_id": location_id,
                    "kind": clean_kind,
                    "controller_id": clean_controller,
                    "address": clean_address,
                    "metadata": dict(row.get("metadata") or {}),
                    "active": bool(row.get("active", True)),
                }
            )
        bindings: list[HardwareBinding] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in normalized:
                location_id = str(row["location_id"])
                exists = conn.execute(
                    "SELECT 1 FROM locations WHERE id = ? AND active = 1",
                    (location_id,),
                ).fetchone()
                if exists is None:
                    raise KeyError(f"unknown location: {location_id}")
            requested_pixels: dict[tuple[str, int], str] = {}
            batch_locations = {str(row["location_id"]) for row in normalized}
            for row in normalized:
                if row["kind"] != "wled_pixels" or not row["active"]:
                    continue
                location_id = str(row["location_id"])
                for pixel in pixels_from_binding(
                    str(row["address"]), dict(row["metadata"])
                ):
                    key = (str(row["controller_id"]), pixel)
                    owner = requested_pixels.get(key)
                    if owner is not None and owner != location_id:
                        raise ValueError(
                            f"WLED pixel {pixel} is assigned more than once in this batch"
                        )
                    requested_pixels[key] = location_id
            existing_rows = conn.execute(
                """
                SELECT location_id, controller_id, address, metadata_json
                FROM hardware_bindings
                WHERE kind = 'wled_pixels' AND active = 1
                """
            ).fetchall()
            for existing in existing_rows:
                if str(existing["location_id"]) in batch_locations:
                    continue
                metadata = json.loads(str(existing["metadata_json"] or "{}"))
                for pixel in pixels_from_binding(str(existing["address"]), metadata):
                    key = (str(existing["controller_id"]), pixel)
                    if key in requested_pixels:
                        raise ValueError(
                            f"WLED pixel {pixel} is already assigned to "
                            f"{existing['location_id']}"
                        )
            for row in normalized:
                location_id = str(row["location_id"])
                clean_kind = str(row["kind"])
                clean_controller = str(row["controller_id"])
                clean_address = str(row["address"])
                data = dict(row["metadata"])
                active = bool(row["active"])
                if active:
                    conn.execute(
                        """
                        UPDATE hardware_bindings
                        SET active = 0, updated_at = CURRENT_TIMESTAMP
                        WHERE location_id = ? AND kind = ? AND controller_id = ?
                        """,
                        (location_id, clean_kind, clean_controller),
                    )
                binding_id = _upsert_hardware_binding(
                    conn,
                    location_id=location_id,
                    kind=clean_kind,
                    controller_id=clean_controller,
                    address=clean_address,
                    metadata=data,
                    active=active,
                )
                if clean_kind == "wled_pixels":
                    pixels = pixels_from_binding(clean_address, data)
                    conn.execute(
                        """
                        UPDATE locations
                        SET light_target = ?, light_indexes = ?
                        WHERE id = ?
                        """,
                        (
                            clean_controller or None,
                            json.dumps(pixels) if pixels and active else None,
                            location_id,
                        ),
                    )
                binding_row = conn.execute(
                    """
                    SELECT id, location_id, kind, controller_id, address,
                           metadata_json, active
                    FROM hardware_bindings
                    WHERE id = ?
                    """,
                    (binding_id,),
                ).fetchone()
                bindings.append(hardware_binding_from_row(binding_row))
        return bindings


def location_from_row(row: sqlite3.Row) -> Location:
    grid = json.loads(row["grid_json"]) if row["grid_json"] else None
    raw_lights = json.loads(row["light_indexes"]) if row["light_indexes"] else []
    keys = row.keys()
    cell_col = row["cell_col"] if "cell_col" in keys else None
    cell_row = row["cell_row"] if "cell_row" in keys else None
    cell_w = row["cell_w"] if "cell_w" in keys else None
    cell_h = row["cell_h"] if "cell_h" in keys else None
    raw_tags = json.loads(row["purpose_tags"]) if "purpose_tags" in keys and row["purpose_tags"] else []
    raw_dimensions = {}
    if "internal_dimensions_json" in keys and row["internal_dimensions_json"]:
        try:
            parsed_dimensions = json.loads(str(row["internal_dimensions_json"]))
            if isinstance(parsed_dimensions, dict):
                raw_dimensions = parsed_dimensions
        except json.JSONDecodeError:
            pass
    return Location(
        id=str(row["id"]),
        type=str(row["type"]),
        parent_id=row["parent_id"],
        label=str(row["label"]),
        grid=grid,
        col=int(cell_col) if cell_col is not None else None,
        row=int(cell_row) if cell_row is not None else None,
        w=int(cell_w) if cell_w is not None else 1,
        h=int(cell_h) if cell_h is not None else 1,
        light_target=row["light_target"],
        light_indexes=[int(index) for index in raw_lights],
        capacity_class=normalize_capacity_class(
            str(row["capacity_class"]) if "capacity_class" in keys else "medium"
        ),
        purpose_tags=[normalize_tag(str(tag)) for tag in raw_tags if normalize_tag(str(tag))],
        placement_notes=str(row["placement_notes"]) if "placement_notes" in keys else "",
        internal_dimensions=normalize_internal_dimensions(raw_dimensions),
        accepts_items=bool(row["accepts_items"]) if "accepts_items" in keys else True,
        active=bool(row["active"]) if "active" in keys else True,
        archived_at=str(row["archived_at"]) if "archived_at" in keys and row["archived_at"] else None,
        archive_group=(
            str(row["archive_group"])
            if "archive_group" in keys and row["archive_group"]
            else None
        ),
    )


BINDING_KINDS = {"wled_pixels", "wled_segment", "eink_tag", "nfc_tag"}


def normalize_binding_kind(value: str | None) -> str:
    clean = (value or "").strip().lower().replace("-", "_")
    return clean if clean in BINDING_KINDS else ""


def pixels_from_binding(address: str, metadata: dict | None = None) -> list[int]:
    data = metadata or {}
    raw_pixels = data.get("pixels")
    if isinstance(raw_pixels, list):
        return sorted({int(pixel) for pixel in raw_pixels})
    parts = [
        part.strip()
        for part in address.replace(";", ",").split(",")
        if part.strip()
    ]
    return sorted({int(part) for part in parts})


def _upsert_hardware_binding(
    conn: sqlite3.Connection,
    *,
    location_id: str,
    kind: str,
    controller_id: str,
    address: str,
    metadata: dict,
    active: bool,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO hardware_bindings(location_id, kind, controller_id, address, metadata_json, active)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(location_id, kind, controller_id, address) DO UPDATE SET
            metadata_json = excluded.metadata_json,
            active = excluded.active,
            updated_at = CURRENT_TIMESTAMP
        RETURNING id
        """,
        (
            location_id,
            kind,
            controller_id,
            address,
            json.dumps(metadata or {}, sort_keys=True),
            1 if active else 0,
        ),
    )
    return int(cursor.fetchone()["id"])


def hardware_binding_from_row(row: sqlite3.Row) -> HardwareBinding:
    try:
        metadata = json.loads(str(row["metadata_json"] or "{}"))
    except json.JSONDecodeError:
        metadata = {}
    return HardwareBinding(
        id=int(row["id"]),
        location_id=str(row["location_id"]),
        kind=str(row["kind"]),
        controller_id=str(row["controller_id"] or ""),
        address=str(row["address"] or ""),
        metadata=metadata if isinstance(metadata, dict) else {},
        active=bool(row["active"]),
    )


CAPACITY_CLASSES = {"tiny", "small", "medium", "large", "oversize"}


def normalize_capacity_class(value: str | None) -> str:
    clean = (value or "medium").strip().lower()
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
    return clean if clean in CAPACITY_CLASSES else "medium"


def normalize_tag(value: str) -> str:
    return "-".join(part for part in value.strip().lower().replace("_", "-").split() if part)


def normalize_internal_dimensions(value: dict | None) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    dimensions: dict[str, float] = {}
    for key in ("width_mm", "depth_mm", "height_mm"):
        try:
            number = float(value.get(key))
        except (TypeError, ValueError):
            continue
        if 0 < number <= 10000:
            dimensions[key] = round(number, 1)
    return dimensions


def generate_cells(
    store: LocationStore,
    parent_id: str,
    cols: int,
    rows: int,
    *,
    cell_type: str = "bin",
    naming: str = "words",
) -> list[str]:
    """Parametric fill: create a positioned 1×1 cell for every slot in a grid.

    A 7×9 drawer becomes 63 bins, each at its own (col, row) — the substrate the
    diagram renders from and the photo / on-demand paths write into. Idempotent
    on the cell ids, so re-filling a grid is safe."""
    created: list[str] = []
    index = 0
    for r in range(1, rows + 1):
        for c in range(1, cols + 1):
            cell_id = f"{parent_id}-c{c}-r{r}"
            label = f"C{c}·R{r}" if naming == "coords" else label_for_index(parent_id, index)
            store.upsert(
                Location(
                    id=cell_id,
                    type=cell_type,
                    parent_id=parent_id,
                    label=label,
                    col=c,
                    row=r,
                    capacity_class="small",
                )
            )
            created.append(cell_id)
            index += 1
    return created


def backfill_from_layout(
    store: LocationStore,
    layout: StorageLayout,
    *,
    cabinet_id: str = "main-cabinet",
    cabinet_label: str | None = None,
    light_target: str = "main-cabinet",
) -> int:
    """Represent the existing cabinet layout as a location tree.

    Creates a `main-cabinet` root and one `bin` child per layout bin, copying
    the bin's LED indexes so the cabinet lights up identically. Idempotent:
    re-running updates rows in place rather than duplicating them.
    """
    existing_cabinet = store.get(cabinet_id, include_archived=True)
    store.upsert(
        Location(
            id=cabinet_id,
            type="cabinet",
            parent_id=None,
            label=cabinet_label or layout.name.replace("-", " ").title(),
            light_target=light_target,
            capacity_class="large",
            purpose_tags=["small-parts", "lit"],
            internal_dimensions=(
                existing_cabinet.internal_dimensions if existing_cabinet else {}
            ),
            accepts_items=(
                existing_cabinet.accepts_items if existing_cabinet else False
            ),
            active=existing_cabinet.active if existing_cabinet else True,
            archived_at=existing_cabinet.archived_at if existing_cabinet else None,
            archive_group=existing_cabinet.archive_group if existing_cabinet else None,
        )
    )
    for bin in layout.bins:
        existing_bin = store.get(bin.id, include_archived=True)
        store.upsert(
            Location(
                id=bin.id,
                type="bin",
                parent_id=cabinet_id,
                label=bin.id,
                light_target=light_target,
                light_indexes=list(bin.led_indexes),
                capacity_class=bin.size,
                purpose_tags=["small-parts"],
                internal_dimensions=(existing_bin.internal_dimensions if existing_bin else {}),
                accepts_items=existing_bin.accepts_items if existing_bin else True,
                active=existing_bin.active if existing_bin else True,
                archived_at=existing_bin.archived_at if existing_bin else None,
                archive_group=existing_bin.archive_group if existing_bin else None,
            )
        )
    return store.count()


def main() -> None:  # pragma: no cover - operational entry point
    import argparse

    from .config import LedConfig

    parser = argparse.ArgumentParser(
        prog="searchable-storage-locations",
        description="Backfill the locations table from the storage layout.",
    )
    parser.add_argument("--config")
    args = parser.parse_args()

    cfg = LedConfig.from_path(args.config)
    layout = StorageLayout.from_path(cfg.layout_path)
    store = LocationStore(cfg.database_path)
    total = backfill_from_layout(store, layout)
    print(f"backfilled {total} locations into {cfg.database_path}")


if __name__ == "__main__":  # pragma: no cover
    main()
