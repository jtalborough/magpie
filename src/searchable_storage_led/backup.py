from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
from typing import Protocol
import urllib.error
import urllib.parse
import urllib.request
import zipfile


BUNDLE_SCHEMA = "magpie.backup.bundle.v1"
PHOTO_URL = re.compile(r"/photos/(?P<id>\d+)")


class BackupError(RuntimeError):
    pass


class BackupSource(Protocol):
    def json(self, path: str) -> dict[str, object]: ...

    def bytes(self, path: str) -> tuple[bytes, str]: ...


@dataclass(frozen=True)
class HttpBackupSource:
    base_url: str
    authorization: str = ""
    workspace_id: str = ""
    timeout: float = 90.0

    def _request(self, path: str) -> urllib.request.Request:
        headers: dict[str, str] = {}
        if self.authorization:
            headers["Authorization"] = self.authorization
        if self.workspace_id:
            headers["X-Magpie-Workspace"] = self.workspace_id
        return urllib.request.Request(
            urllib.parse.urljoin(self.base_url.rstrip("/") + "/", path.lstrip("/")),
            headers=headers,
        )

    def json(self, path: str) -> dict[str, object]:
        try:
            with urllib.request.urlopen(
                self._request(path), timeout=self.timeout
            ) as response:
                payload = json.load(response)
        except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            raise BackupError(f"could not read {path}: {type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise BackupError(f"{path} did not return an object")
        return payload

    def bytes(self, path: str) -> tuple[bytes, str]:
        try:
            with urllib.request.urlopen(
                self._request(path), timeout=self.timeout
            ) as response:
                content = response.read()
                content_type = response.headers.get_content_type()
        except (OSError, urllib.error.HTTPError) as exc:
            raise BackupError(f"could not read {path}: {type(exc).__name__}") from exc
        if not content:
            raise BackupError(f"{path} returned no content")
        return content, content_type


def _object_rows(payload: dict[str, object], key: str) -> list[dict[str, object]]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        raise BackupError(f"{key} is not a list")
    if any(not isinstance(row, dict) for row in value):
        raise BackupError(f"{key} contains a non-object row")
    return [dict(row) for row in value]


def _photo_id(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    match = PHOTO_URL.fullmatch(value)
    return int(match.group("id")) if match else None


def _photo_suffix(content_type: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/heic": ".heic",
        "image/heif": ".heif",
    }.get(content_type, mimetypes.guess_extension(content_type) or ".bin")


def _validate_graph(
    locations: list[dict[str, object]],
    inventory: list[dict[str, object]],
    placement_rules: list[dict[str, object]],
) -> set[str]:
    ids = [str(row.get("id") or "").strip() for row in locations]
    location_ids = set(ids)
    if not ids or "" in location_ids:
        raise BackupError("location graph contains a missing id")
    if len(ids) != len(location_ids):
        raise BackupError("location graph contains duplicate ids")
    for row in locations:
        parent = str(row.get("parent_id") or "").strip()
        if parent and parent not in location_ids:
            raise BackupError(f"location {row.get('id')} has an unknown parent")
    for row in inventory:
        if str(row.get("bin_id") or "").strip() not in location_ids:
            raise BackupError("inventory contains an unknown location")
    for row in placement_rules:
        if str(row.get("target_location_id") or "").strip() not in location_ids:
            raise BackupError("placement rule contains an unknown destination")
    return location_ids


def create_backup_bundle(
    source: BackupSource,
    output_path: str | Path,
) -> dict[str, object]:
    build = source.json("/build")
    location_payload = source.json("/locations?include_archived=true")
    active_payload = source.json("/inventory")
    archived_payload = source.json("/inventory?archived=true")
    rules_payload = source.json("/rules")
    locations = _object_rows(location_payload, "locations")
    active_inventory = _object_rows(active_payload, "items")
    archived_inventory = _object_rows(archived_payload, "items")
    inventory_by_placement: dict[tuple[str, str], dict[str, object]] = {}
    for row in [*active_inventory, *archived_inventory]:
        key = (
            str(row.get("item_id") or row.get("name") or ""),
            str(row.get("bin_id") or ""),
        )
        inventory_by_placement[key] = row
    inventory = list(inventory_by_placement.values())
    placement_rules = _object_rows(rules_payload, "placement_rules")
    size_rules = _object_rows(rules_payload, "size_rules")
    _validate_graph(locations, inventory, placement_rules)

    photo_metadata: dict[int, dict[str, object]] = {}
    referenced_photo_ids: set[int] = set()
    for row in locations:
        photos = row.get("photos") or []
        if not isinstance(photos, list):
            raise BackupError(f"location {row.get('id')} photos is not a list")
        row_ids: list[int] = []
        for photo in photos:
            if not isinstance(photo, dict):
                raise BackupError("location photos contains a non-object row")
            photo_id = int(photo["id"])
            row_ids.append(photo_id)
            referenced_photo_ids.add(photo_id)
            photo_metadata[photo_id] = {
                "filename": str(photo.get("filename") or f"photo-{photo_id}"),
                "content_type": str(
                    photo.get("content_type") or "application/octet-stream"
                ),
                "item_id": photo.get("item_id"),
                "location_id": photo.get("location_id"),
            }
        row["photo_ids"] = list(dict.fromkeys(row_ids))
        row.pop("photos", None)
    for row in inventory:
        photos = row.get("photos") or []
        if not isinstance(photos, list):
            raise BackupError("inventory photos is not a list")
        row_ids = [
            photo_id
            for photo_id in (_photo_id(value) for value in photos)
            if photo_id is not None
        ]
        referenced_photo_ids.update(row_ids)
        row["photo_ids"] = list(dict.fromkeys(row_ids))
        row.pop("photos", None)

    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise BackupError(f"backup output already exists: {output}")
    temporary = output.with_name(f".{output.name}.partial")
    photo_rows: list[dict[str, object]] = []
    photo_payloads: dict[str, bytes] = {}
    for photo_id in sorted(referenced_photo_ids):
        content, response_type = source.bytes(f"/photos/{photo_id}")
        metadata = photo_metadata.get(photo_id, {})
        content_type = str(metadata.get("content_type") or response_type)
        bundle_path = f"photos/{photo_id}{_photo_suffix(content_type)}"
        digest = hashlib.sha256(content).hexdigest()
        photo_payloads[bundle_path] = content
        photo_rows.append(
            {
                "id": photo_id,
                "filename": str(
                    metadata.get("filename") or Path(bundle_path).name
                ),
                "content_type": content_type,
                "bundle_path": bundle_path,
                "bytes": len(content),
                "sha256": digest,
            }
        )

    manifest = {
        "schema": BUNDLE_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "build": build,
        "snapshot": {
            "schema": "magpie.inventory.v1",
            "build": build,
            "locations": locations,
            "inventory": inventory,
            "photos": photo_rows,
            "placement_rules": placement_rules,
            "size_rules": size_rules,
        },
    }
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
            )
            for path, content in photo_payloads.items():
                archive.writestr(path, content)
        os.chmod(temporary, 0o600)
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return verify_backup_bundle(output)


def verify_backup_bundle(path: str | Path) -> dict[str, object]:
    bundle_path = Path(path).expanduser().resolve()
    try:
        with zipfile.ZipFile(bundle_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("schema") != BUNDLE_SCHEMA:
                raise BackupError("unexpected backup bundle schema")
            snapshot = manifest.get("snapshot")
            if not isinstance(snapshot, dict):
                raise BackupError("backup bundle has no snapshot")
            locations = _object_rows(snapshot, "locations")
            inventory = _object_rows(snapshot, "inventory")
            photos = _object_rows(snapshot, "photos")
            placement_rules = _object_rows(snapshot, "placement_rules")
            size_rules = _object_rows(snapshot, "size_rules")
            _validate_graph(locations, inventory, placement_rules)
            expected_photo_ids = {
                int(photo_id)
                for row in [*locations, *inventory]
                for photo_id in (row.get("photo_ids") or [])
            }
            actual_photo_ids: set[int] = set()
            photo_bytes = 0
            for row in photos:
                photo_id = int(row["id"])
                if photo_id in actual_photo_ids:
                    raise BackupError("backup bundle contains duplicate photos")
                content = archive.read(str(row["bundle_path"]))
                if len(content) != int(row["bytes"]):
                    raise BackupError(f"photo {photo_id} size does not match")
                if hashlib.sha256(content).hexdigest() != str(row["sha256"]):
                    raise BackupError(f"photo {photo_id} checksum does not match")
                actual_photo_ids.add(photo_id)
                photo_bytes += len(content)
            if expected_photo_ids != actual_photo_ids:
                raise BackupError("backup bundle photo references do not match")
    except (KeyError, OSError, ValueError, zipfile.BadZipFile) as exc:
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"invalid backup bundle: {type(exc).__name__}") from exc
    return {
        "ok": True,
        "schema": BUNDLE_SCHEMA,
        "path": str(bundle_path),
        "bytes": bundle_path.stat().st_size,
        "sha256": hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
        "build_commit": str((manifest.get("build") or {}).get("commit") or ""),
        "locations": len(locations),
        "inventory_placements": len(inventory),
        "unique_items": len(
            {
                str(row.get("item_id") or row.get("name") or "")
                for row in inventory
            }
        ),
        "photos": len(photos),
        "photo_bytes": photo_bytes,
        "placement_rules": len(placement_rules),
        "size_rules": len(size_rules),
        "mode": oct(bundle_path.stat().st_mode & 0o777),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create or verify a photo-inclusive Magpie backup bundle."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--url", required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument(
        "--authorization-env",
        default="",
        help="Environment variable containing the complete Authorization header",
    )
    create.add_argument("--workspace-id", default="")
    create.add_argument("--timeout", type=float, default=90.0)
    verify = subparsers.add_parser("verify")
    verify.add_argument("bundle", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "create":
            authorization = (
                os.environ.get(args.authorization_env, "")
                if args.authorization_env
                else ""
            )
            if args.authorization_env and not authorization:
                raise BackupError(
                    f"authorization environment variable {args.authorization_env} is empty"
                )
            result = create_backup_bundle(
                HttpBackupSource(
                    base_url=args.url,
                    authorization=authorization,
                    workspace_id=args.workspace_id,
                    timeout=args.timeout,
                ),
                args.output,
            )
        else:
            result = verify_backup_bundle(args.bundle)
    except BackupError as exc:
        parser.exit(1, f"magpie-backup: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
