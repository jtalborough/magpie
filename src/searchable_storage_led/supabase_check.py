from __future__ import annotations

import argparse
import json
import os
from typing import Protocol

from .config import LedConfig
from .supabase_client import SupabaseRestClient


REQUIRED_TABLES = (
    "workspace_members",
    "locations",
    "items",
    "item_aliases",
    "inventory_entries",
    "photos",
    "inventory_review_photos",
    "repository_events",
    "hardware_bindings",
    "placement_rules",
    "size_rules",
    "agent_examples",
    "assistant_interactions",
    "active_guidance_states",
    "voice_attempts",
)
EDGE_TABLES = ("edge_devices", "edge_commands")
MULTI_WORKSPACE_TABLES = (
    "workspaces",
    *REQUIRED_TABLES,
)
REQUIRED_COLUMNS = {
    "active_guidance_states": "workspace_id,state,updated_at",
    "voice_attempts": "id,device_id,runtime_version",
    "edge_devices": (
        "workspace_id,device_id,credential_hash,credential_scopes,"
        "pairing_hash,pairing_scopes,pairing_expires_at"
    ),
}
ATTACHMENT_COUNT_TABLES = (
    "locations",
    "items",
    "inventory_entries",
    "photos",
    "placement_rules",
    "size_rules",
)
REQUIRED_CAPABILITIES = {
    "atomic_location_creation": True,
    "atomic_location_lifecycle": True,
    "atomic_hardware_bindings": True,
    "atomic_inventory_import": True,
    "archive_group_provenance": True,
    "guided_location_audits": True,
    "location_content_revisions": True,
}


def _columns_for(table: str) -> str:
    return REQUIRED_COLUMNS.get(table, "*")


class SchemaClient(Protocol):
    def select(self, table: str, **kwargs) -> list[dict[str, object]]: ...

    def rpc(
        self, function: str, arguments: dict[str, object] | None = None
    ) -> object: ...


def check_supabase_capabilities(client: SchemaClient) -> dict[str, object]:
    result = client.rpc("magpie_schema_capabilities")
    if not isinstance(result, dict):
        raise RuntimeError("Supabase schema capabilities returned an invalid response")
    if result.get("schema") != "magpie.supabase_capabilities.v1":
        raise RuntimeError("Supabase schema capability contract is missing or outdated")
    features = result.get("features")
    if not isinstance(features, dict):
        raise RuntimeError("Supabase schema capability features are missing")
    missing = sorted(
        name
        for name, expected in REQUIRED_CAPABILITIES.items()
        if features.get(name) is not expected
    )
    if missing:
        raise RuntimeError(
            "Supabase schema is missing required capabilities: " + ", ".join(missing)
        )
    return {
        "schema": str(result["schema"]),
        "features": {name: features[name] for name in REQUIRED_CAPABILITIES},
    }


def check_supabase_schema(
    client: SchemaClient,
    *,
    include_edge: bool = False,
) -> dict[str, object]:
    checked: list[str] = []
    tables = (*MULTI_WORKSPACE_TABLES, *(EDGE_TABLES if include_edge else ()))
    for table in tables:
        client.select(table, columns=_columns_for(table), limit=1)
        checked.append(table)
    capabilities = check_supabase_capabilities(client)
    return {
        "schema": "magpie.supabase_check.v2",
        "ready": True,
        "mode": "multi_workspace",
        "tables": checked,
        "table_count": len(checked),
        "capabilities": capabilities,
    }


def check_supabase(
    client: SchemaClient,
    *,
    workspace_id: str,
    include_edge: bool = False,
) -> dict[str, object]:
    workspace = client.select(
        "workspaces",
        columns="id,name",
        filters={"id": f"eq.{workspace_id}"},
        limit=1,
    )
    if not workspace:
        raise RuntimeError(f"Supabase workspace is not accessible: {workspace_id}")
    checked: list[str] = []
    tables = (*REQUIRED_TABLES, *(EDGE_TABLES if include_edge else ()))
    for table in tables:
        filters = None if table == "item_aliases" else {
            "workspace_id": f"eq.{workspace_id}"
        }
        client.select(
            table,
            columns=_columns_for(table),
            filters=filters,
            limit=1,
        )
        checked.append(table)
    capabilities = check_supabase_capabilities(client)
    return {
        "schema": "magpie.supabase_check.v2",
        "ready": True,
        "workspace": {
            "id": workspace_id,
            "name": str(workspace[0].get("name") or ""),
        },
        "tables": checked,
        "table_count": len(checked),
        "capabilities": capabilities,
    }


def check_existing_workspace_attachment(
    client: SelectClient,
    *,
    workspace_id: str,
    include_edge: bool = False,
) -> dict[str, object]:
    """Verify that an existing workspace is ready for a hosted service."""
    checked = check_supabase(
        client,
        workspace_id=workspace_id,
        include_edge=include_edge,
    )
    workspace = client.select(
        "workspaces",
        columns="id,name,owner_id",
        filters={"id": f"eq.{workspace_id}"},
        limit=1,
    )[0]
    owner_id = str(workspace.get("owner_id") or "").strip()
    if not owner_id:
        raise RuntimeError("Supabase workspace has no owner")

    memberships = client.select(
        "workspace_members",
        columns="user_id,role",
        filters={"workspace_id": f"eq.{workspace_id}"},
        limit=10_000,
    )
    role_counts: dict[str, int] = {}
    owner_membership_matches = False
    for membership in memberships:
        role = str(membership.get("role") or "unknown")
        role_counts[role] = role_counts.get(role, 0) + 1
        if (
            role == "owner"
            and str(membership.get("user_id") or "").strip() == owner_id
        ):
            owner_membership_matches = True
    if not owner_membership_matches:
        raise RuntimeError(
            "Supabase workspace owner does not have a matching owner membership"
        )

    data_counts: dict[str, int] = {}
    for table in ATTACHMENT_COUNT_TABLES:
        rows = client.select(
            table,
            columns="id",
            filters={"workspace_id": f"eq.{workspace_id}"},
            limit=10_000,
        )
        data_counts[table] = len(rows)

    return {
        "schema": "magpie.supabase_attachment_check.v1",
        "ready": True,
        "workspace": checked["workspace"],
        "membership": {
            "total": len(memberships),
            "roles": dict(sorted(role_counts.items())),
            "owner_matches_workspace": True,
        },
        "data_counts": data_counts,
        "tables": checked["tables"],
        "table_count": checked["table_count"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="searchable-storage-supabase-check",
        description="Verify Magpie's configured Supabase workspace and schema.",
    )
    parser.add_argument("--config")
    parser.add_argument(
        "--verify-attachment",
        action="store_true",
        help=(
            "Verify an existing workspace's owner membership and summarize its "
            "data without printing user identifiers"
        ),
    )
    parser.add_argument(
        "--workspace-id",
        default="",
        help="Existing workspace to verify (defaults to storage.workspace_id)",
    )
    args = parser.parse_args()
    config = LedConfig.from_path(args.config)
    storage = config.storage
    if storage.backend != "supabase":
        raise SystemExit("storage.backend must be supabase")
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
    workspace_id = args.workspace_id.strip() or storage.workspace_id
    if args.verify_attachment:
        if not workspace_id:
            raise SystemExit(
                "--workspace-id is required when the config is request-scoped"
            )
        result = check_existing_workspace_attachment(
            client,
            workspace_id=workspace_id,
            include_edge=config.edge.mode in {"central", "workspace"},
        )
    elif config.auth.multi_workspace:
        result = check_supabase_schema(
            client,
            include_edge=config.edge.mode in {"central", "workspace"},
        )
    else:
        result = check_supabase(
            client,
            workspace_id=workspace_id,
            include_edge=config.edge.mode in {"central", "workspace"},
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
