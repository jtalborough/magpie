from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

import pytest

from searchable_storage_led.locations import Location, LocationStore
from searchable_storage_led.store import InventoryStore
from searchable_storage_led.supabase_check import REQUIRED_TABLES
from searchable_storage_led.supabase_migrate import (
    SqliteToSupabaseMigrator,
    main as migrate_main,
    resolve_source_path,
)


def _insert_legacy_location(
    database_path, *, location_id: str, location_type: str, label: str
) -> None:
    """Create an invalid pre-uniqueness location fixture."""
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO locations(id, type, label) VALUES (?, ?, ?)",
            (location_id, location_type, label),
        )


class MigrationTarget:
    def __init__(self) -> None:
        self.tables = {table: [] for table in ["workspaces", *REQUIRED_TABLES]}
        self.tables["workspaces"] = [
            {"id": "workspace-1", "name": "Main workshop"}
        ]
        self.objects = {}
        self.rpc_calls = []
        self.batch_sizes = []

    def select(self, table, *, filters=None, limit=None, **_kwargs):
        rows = [deepcopy(row) for row in self.tables[table] if self._matches(row, filters)]
        return rows[:limit] if limit is not None else rows

    def upsert(self, table, values, *, on_conflict, return_rows=True):
        if isinstance(values, list):
            self.batch_sizes.append((table, len(values)))
            rows = []
            for value in values:
                rows.extend(
                    self.upsert(
                        table,
                        value,
                        on_conflict=on_conflict,
                        return_rows=True,
                    )
                )
            return rows if return_rows else []
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
            existing = deepcopy(values)
            existing.setdefault("id", str(uuid4()))
            self.tables[table].append(existing)
        else:
            existing.update(deepcopy(values))
        return [deepcopy(existing)] if return_rows else []

    def update(self, table, values, *, filters, return_rows=True):
        updated = []
        for row in self.tables[table]:
            if self._matches(row, filters):
                row.update(deepcopy(values))
                updated.append(deepcopy(row))
        return updated if return_rows else []

    def upload_object(self, *, bucket, path, data, content_type, upsert=False):
        if not upsert and (bucket, path) in self.objects:
            raise RuntimeError("duplicate object")
        self.objects[(bucket, path)] = (data, content_type)
        return {"Key": f"{bucket}/{path}"}

    def rpc(self, function, arguments=None):
        self.rpc_calls.append((function, arguments or {}))
        if function == "magpie_schema_capabilities":
            return {
                "schema": "magpie.supabase_capabilities.v1",
                "features": {
                    "atomic_location_creation": True,
                    "atomic_location_lifecycle": True,
                    "atomic_hardware_bindings": True,
                    "atomic_inventory_import": True,
                    "archive_group_provenance": True,
                    "guided_location_audits": True,
                    "location_content_revisions": True,
                },
            }
        if function == "magpie_workspace_counts":
            return {
                table: len(rows)
                for table, rows in self.tables.items()
                if table != "workspaces"
            }
        return None

    @staticmethod
    def _matches(row, filters):
        for key, expression in (filters or {}).items():
            operator, expected = expression.split(".", 1)
            if operator == "eq" and str(row.get(key)) != expected:
                return False
        return True


def _source_database(tmp_path):
    path = tmp_path / "inventory.db"
    inventory = InventoryStore(path)
    locations = LocationStore(path)
    locations.upsert(
        Location(
            id="main-cabinet",
            type="cabinet",
            label="Main cabinet",
            light_target="main",
            light_indexes=[0, 1],
        )
    )
    locations.upsert(
        Location(
            id="B001",
            type="bin",
            parent_id="main-cabinet",
            label="Small parts",
            capacity_class="small",
        )
    )
    item_id = inventory.upsert_item(
        name="M4 T-nuts",
        description="Pronged threaded inserts",
        category="hardware",
        aliases=["tee nuts"],
    )
    photo = inventory.add_photo(
        filename="t-nuts.jpeg",
        content_type="image/jpeg",
        data=b"photo-bytes",
    )
    inventory.attach_photo(item_id=item_id, photo_id=photo.id)
    inventory.put_inventory(
        item_id=item_id,
        bin_id="B001",
        quantity=12,
        unit="pieces",
        needs_review=True,
        review_reason="confirm thread size",
    )
    inventory.link_review_photo(item_id=item_id, bin_id="B001", photo_id=photo.id)
    inventory.put_inventory(
        item_id=item_id,
        bin_id="garage-box",
        quantity=2,
        unit="pieces",
        normalize_bin=False,
    )
    inventory.update_item_size(
        item_id=item_id,
        size_class="small",
        dimensions={"length_mm": 8},
    )
    inventory.learn_rule(
        rule_text="T-nuts stay in the cabinet",
        trigger_terms=["t-nuts", "hardware"],
        target_location_id="B001",
    )
    inventory.learn_size_rule(
        rule_text="T-nuts are small",
        trigger_terms=["t-nuts"],
        size_class="small",
    )
    inventory.record_example(
        command="Put away T-nuts",
        action='[{"tool":"put_item"}]',
        kind="confirmed",
        context={"source": "voice"},
    )
    inventory.record_assistant_interaction(
        route="/assistant/confirm",
        command="Confirm",
        photo_ids=[photo.id],
        response={"applied": [{"action": "put_item"}]},
        status="confirmed",
    )
    inventory.record_voice_attempt(
        transcript="Magpie find T-nuts",
        wake_word="magpie",
        wake_heard=True,
        command="find T-nuts",
        query="T-nuts",
        backend_response={"mode": "find"},
    )
    return path, item_id, photo.id


def test_dry_run_reports_source_without_touching_target(tmp_path) -> None:
    path, _, _ = _source_database(tmp_path)
    target = MigrationTarget()
    source_before = path.read_bytes()

    summary = SqliteToSupabaseMigrator(
        source_path=path,
        client=target,
        workspace_id="workspace-1",
    ).run(dry_run=True)

    assert summary["dry_run"] is True
    assert summary["source_counts"]["items"] == 1
    assert summary["source_counts"]["photos"] == 1
    assert summary["placeholder_locations"] == ["garage-box"]
    assert summary["location_name_conflicts"] == []
    assert all(not rows for table, rows in target.tables.items() if table != "workspaces")
    assert target.objects == {}
    assert path.read_bytes() == source_before


def test_migration_rejects_ambiguous_active_sibling_names_before_writes(
    tmp_path,
) -> None:
    path, _, _ = _source_database(tmp_path)
    _insert_legacy_location(
        path,
        location_id="main-cabinet-2",
        location_type="cabinet",
        label="  MAIN   cabinet ",
    )
    target = MigrationTarget()
    migrator = SqliteToSupabaseMigrator(
        source_path=path,
        client=target,
        workspace_id="workspace-1",
    )

    preview = migrator.run(dry_run=True)

    assert preview["ready"] is False
    assert preview["location_name_conflicts"] == [
        {
            "parent_id": None,
            "label": "Main cabinet",
            "location_ids": ["main-cabinet", "main-cabinet-2"],
        }
    ]
    with pytest.raises(
        ValueError,
        match="ambiguous active sibling location names",
    ):
        migrator.run()
    assert all(
        not rows for table, rows in target.tables.items() if table != "workspaces"
    )
    assert target.rpc_calls == []


def test_migration_writes_real_parents_before_same_named_children(tmp_path) -> None:
    path = tmp_path / "inventory.db"
    InventoryStore(path)
    locations = LocationStore(path)
    for root_id in ("garage", "attic"):
        locations.upsert(
            Location(
                id=root_id,
                type="room",
                label=root_id.title(),
            )
        )
        locations.upsert(
            Location(
                id=f"{root_id}-hardware",
                parent_id=root_id,
                type="bin",
                label="Hardware",
            )
        )
    target = MigrationTarget()

    summary = SqliteToSupabaseMigrator(
        source_path=path,
        client=target,
        workspace_id="workspace-1",
    ).run()

    assert summary["ready"] is True
    roots = {
        row["local_code"]: row
        for row in target.tables["locations"]
        if row["parent_id"] is None
    }
    children = [
        row
        for row in target.tables["locations"]
        if row["parent_id"] is not None
    ]
    assert len(children) == 2
    assert {
        (row["label"], row["parent_id"])
        for row in children
    } == {
        ("Hardware", roots["garage"]["id"]),
        ("Hardware", roots["attic"]["id"]),
    }


def test_migration_preserves_location_archive_groups(tmp_path) -> None:
    path = tmp_path / "inventory.db"
    InventoryStore(path)
    locations = LocationStore(path)
    locations.upsert(Location(id="attic", type="room", label="Attic"))
    locations.upsert(
        Location(id="retired", type="box", parent_id="attic", label="Retired")
    )
    locations.upsert(
        Location(id="current", type="box", parent_id="attic", label="Current")
    )
    locations.archive_subtree("retired")
    locations.archive_subtree("attic")
    target = MigrationTarget()

    summary = SqliteToSupabaseMigrator(
        source_path=path,
        client=target,
        workspace_id="workspace-1",
    ).run()

    assert summary["ready"] is True
    groups = {
        row["local_code"]: row["archive_group"]
        for row in target.tables["locations"]
    }
    assert groups == {
        "attic": "attic",
        "current": "attic",
        "retired": "retired",
    }


def test_migration_preserves_deleted_location_events_without_resurrecting_place(
    tmp_path,
) -> None:
    path = tmp_path / "inventory.db"
    inventory = InventoryStore(path)
    locations = LocationStore(path)
    locations.upsert(
        Location(
            id="temporary-room",
            type="room",
            label="Temporary room",
        )
    )
    locations.record_event(
        event_type="location_created",
        location_id="temporary-room",
        note="room in",
    )
    locations.delete_subtree("temporary-room")
    target = MigrationTarget()
    migrator = SqliteToSupabaseMigrator(
        source_path=path,
        client=target,
        workspace_id="workspace-1",
    )

    preview = migrator.run(dry_run=True)
    summary = migrator.run()

    assert preview["placeholder_locations"] == []
    assert summary["placeholder_locations"] == []
    assert target.tables["locations"] == []
    assert len(target.tables["repository_events"]) == 1
    event = target.tables["repository_events"][0]
    assert event["event_type"] == "location_created"
    assert event["location_id"] is None
    assert event["location_code"] == "temporary-room"
    assert event["note"] == "room in"


def test_migration_preserves_ids_relationships_photos_and_history(tmp_path) -> None:
    path, item_id, photo_id = _source_database(tmp_path)
    target = MigrationTarget()
    migrator = SqliteToSupabaseMigrator(
        source_path=path,
        client=target,
        workspace_id="workspace-1",
    )

    summary = migrator.run()

    assert summary["ready"] is True
    assert summary["placeholder_locations"] == ["garage-box"]
    assert summary["migrated"]["items"] == 1
    assert summary["migrated"]["photos"] == 1
    item = target.tables["items"][0]
    assert item["local_id"] == item_id
    assert item["dimensions"] == {"length_mm": 8}
    assert target.tables["item_aliases"][0]["alias"] == "tee nuts"
    entries = target.tables["inventory_entries"]
    assert {entry["quantity"] for entry in entries} == {12.0, 2.0}
    assert any(entry["needs_review"] for entry in entries)
    photo = target.tables["photos"][0]
    assert photo["local_id"] == photo_id
    assert photo["item_id"] == item["id"]
    assert list(target.objects.values()) == [(b"photo-bytes", "image/jpeg")]
    assert len(target.tables["inventory_review_photos"]) == 1
    assert target.tables["placement_rules"][0]["local_id"] == 1
    assert target.tables["size_rules"][0]["local_id"] == 1
    assert target.tables["agent_examples"][0]["local_id"] == 1
    assert target.tables["assistant_interactions"][0]["photo_local_ids"] == [photo_id]
    assert target.tables["voice_attempts"][0]["wake_heard"] is True
    assert target.tables["repository_events"]
    assert target.rpc_calls == [
        ("magpie_schema_capabilities", {}),
        ("advance_magpie_identity_sequences", {}),
        ("magpie_workspace_counts", {"target_workspace_id": "workspace-1"}),
    ]

    counts_before = {table: len(rows) for table, rows in target.tables.items()}
    second = migrator.run()
    assert second["ready"] is True
    assert {table: len(rows) for table, rows in target.tables.items()} == counts_before
    assert target.rpc_calls == [
        ("magpie_schema_capabilities", {}),
        ("advance_magpie_identity_sequences", {}),
        ("magpie_workspace_counts", {"target_workspace_id": "workspace-1"}),
        ("magpie_schema_capabilities", {}),
        ("advance_magpie_identity_sequences", {}),
        ("magpie_workspace_counts", {"target_workspace_id": "workspace-1"}),
    ]


def test_missing_photo_is_reported_and_not_migrated(tmp_path) -> None:
    path, _, photo_id = _source_database(tmp_path)
    inventory = InventoryStore(path)
    photo = inventory.photo(photo_id)
    assert photo is not None
    photo_path = photo.path
    Path(photo_path).unlink()
    target = MigrationTarget()

    summary = SqliteToSupabaseMigrator(
        source_path=path,
        client=target,
        workspace_id="workspace-1",
    ).run()

    assert summary["ready"] is False
    assert "missing source file" in summary["warnings"][0]
    assert target.tables["photos"] == []


def test_relative_database_path_resolves_from_installed_app_root(tmp_path) -> None:
    config = tmp_path / "app" / "config" / "led-controller.json"

    assert resolve_source_path(
        config_path=str(config),
        configured_path="state/inventory.db",
    ) == tmp_path / "app" / "state" / "inventory.db"
    assert resolve_source_path(
        config_path=str(config),
        configured_path="state/inventory.db",
        override="backup.db",
    ) == Path("backup.db")


def _multi_workspace_config(tmp_path: Path, database: Path) -> Path:
    config = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "config/container-supabase.example.json"
        ).read_text(encoding="utf-8")
    )
    config["database_path"] = str(database)
    config["storage"]["supabase_url"] = "https://project.supabase.co"
    config["auth"]["supabase_url"] = "https://project.supabase.co"
    config["edge"]["machine_url"] = "https://edge.example"
    path = tmp_path / "multi-workspace.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_cli_dry_run_accepts_explicit_workspace_for_request_scoped_config(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    database = tmp_path / "inventory.db"
    InventoryStore(database)
    LocationStore(database)
    config = _multi_workspace_config(tmp_path, database)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "searchable-storage-migrate-supabase",
            "--config",
            str(config),
            "--database",
            str(database),
            "--workspace-id",
            "workspace-cli",
            "--dry-run",
        ],
    )

    migrate_main()

    assert json.loads(capsys.readouterr().out)["workspace_id"] == "workspace-cli"


def test_cli_requires_explicit_workspace_before_credentials(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "inventory.db"
    InventoryStore(database)
    LocationStore(database)
    config = _multi_workspace_config(tmp_path, database)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "searchable-storage-migrate-supabase",
            "--config",
            str(config),
            "--database",
            str(database),
        ],
    )

    with pytest.raises(
        SystemExit,
        match="--workspace-id is required when storage.workspace_id is not configured",
    ):
        migrate_main()


def test_relative_photo_path_resolves_from_database_app_root(tmp_path) -> None:
    database = tmp_path / "app" / "state" / "inventory.db"
    photo = tmp_path / "app" / "state" / "photos" / "capture.jpg"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"photo")
    migrator = SqliteToSupabaseMigrator(
        source_path=database,
        client=None,
        workspace_id="workspace-1",
    )

    assert migrator._photo_source_path("state/photos/capture.jpg") == photo


def test_large_voice_history_is_upserted_in_bounded_batches(tmp_path) -> None:
    path = tmp_path / "inventory.db"
    inventory = InventoryStore(path)
    LocationStore(path)
    for index in range(205):
        inventory.record_voice_attempt(
            transcript=f"attempt {index}",
            command=f"find item {index}",
        )
    target = MigrationTarget()

    summary = SqliteToSupabaseMigrator(
        source_path=path,
        client=target,
        workspace_id="workspace-1",
        batch_size=100,
    ).run()

    assert summary["migrated"]["voice_attempts"] == 205
    assert [
        size for table, size in target.batch_sizes if table == "voice_attempts"
    ] == [100, 100, 5]
    assert len(target.tables["voice_attempts"]) == 205


def test_migration_fails_when_target_count_audit_is_short() -> None:
    migrated = {"items": 4, "photos": 2}

    with pytest.raises(RuntimeError, match="items"):
        SqliteToSupabaseMigrator._verify_counts(
            migrated,
            {"items": 3, "photos": 2},
        )
