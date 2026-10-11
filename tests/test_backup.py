from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest

from searchable_storage_led.backup import (
    BUNDLE_SCHEMA,
    BackupError,
    create_backup_bundle,
    verify_backup_bundle,
)


class FakeBackupSource:
    def __init__(self) -> None:
        self.payloads = {
            "/build": {
                "version": "0.1.0",
                "commit": "backup-test",
                "source": "test",
            },
            "/locations?include_archived=true": {
                "roots": ["workshop"],
                "locations": [
                    {
                        "id": "workshop",
                        "type": "room",
                        "label": "Workshop",
                        "parent_id": None,
                        "photos": [],
                    },
                    {
                        "id": "parts-bin",
                        "type": "bin",
                        "label": "Parts bin",
                        "parent_id": "workshop",
                        "photos": [
                            {
                                "id": 2,
                                "filename": "parts.png",
                                "content_type": "image/png",
                                "location_id": "parts-bin",
                                "url": "/photos/2",
                            }
                        ],
                    },
                ],
            },
            "/inventory": {
                "items": [
                    {
                        "item_id": 7,
                        "name": "M3 screws",
                        "bin_id": "parts-bin",
                        "quantity": 12,
                        "unit": "each",
                        "photos": ["/photos/1"],
                    }
                ]
            },
            "/inventory?archived=true": {"items": []},
            "/rules": {
                "placement_rules": [
                    {
                        "rule_text": "Screws go in parts.",
                        "target_location_id": "parts-bin",
                        "active": True,
                    }
                ],
                "size_rules": [
                    {
                        "rule_text": "M3 screws are tiny.",
                        "size_class": "tiny",
                        "active": False,
                    }
                ],
            },
        }
        self.photos = {
            "/photos/1": (b"item-photo", "image/jpeg"),
            "/photos/2": (b"location-photo", "image/png"),
        }

    def json(self, path: str) -> dict[str, object]:
        return self.payloads[path]

    def bytes(self, path: str) -> tuple[bytes, str]:
        return self.photos[path]


def test_backup_bundle_preserves_graph_rules_and_individual_photos(tmp_path) -> None:
    output = tmp_path / "magpie-backup.zip"

    summary = create_backup_bundle(FakeBackupSource(), output)

    assert summary == verify_backup_bundle(output)
    assert summary["schema"] == BUNDLE_SCHEMA
    assert summary["build_commit"] == "backup-test"
    assert summary["locations"] == 2
    assert summary["inventory_placements"] == 1
    assert summary["unique_items"] == 1
    assert summary["photos"] == 2
    assert summary["photo_bytes"] == len(b"item-photo") + len(b"location-photo")
    assert summary["placement_rules"] == 1
    assert summary["size_rules"] == 1
    assert summary["mode"] == "0o600"
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            "manifest.json",
            "photos/1.jpg",
            "photos/2.png",
        }
        manifest = json.loads(archive.read("manifest.json"))
    snapshot = manifest["snapshot"]
    assert snapshot["locations"][1]["photo_ids"] == [2]
    assert "photos" not in snapshot["locations"][1]
    assert snapshot["inventory"][0]["photo_ids"] == [1]
    assert "photos" not in snapshot["inventory"][0]
    assert snapshot["size_rules"][0]["active"] is False


def test_backup_bundle_rejects_dangling_inventory_location(tmp_path) -> None:
    source = FakeBackupSource()
    source.payloads["/inventory"]["items"][0]["bin_id"] = "missing"

    with pytest.raises(BackupError, match="unknown location"):
        create_backup_bundle(source, tmp_path / "bad.zip")


def test_backup_bundle_refuses_to_overwrite_existing_backup(tmp_path) -> None:
    output = tmp_path / "magpie-backup.zip"
    create_backup_bundle(FakeBackupSource(), output)

    with pytest.raises(BackupError, match="already exists"):
        create_backup_bundle(FakeBackupSource(), output)


def test_backup_bundle_verifier_rejects_modified_photo(tmp_path) -> None:
    output = tmp_path / "magpie-backup.zip"
    create_backup_bundle(FakeBackupSource(), output)
    rewritten = tmp_path / "rewritten.zip"
    with zipfile.ZipFile(output) as source, zipfile.ZipFile(rewritten, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            target.writestr(
                name, b"changed!!!" if name == "photos/1.jpg" else content
            )

    with pytest.raises(BackupError, match="checksum"):
        verify_backup_bundle(rewritten)
