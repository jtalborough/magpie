import json

import pytest

from searchable_storage_led.supabase_check import (
    ATTACHMENT_COUNT_TABLES,
    EDGE_TABLES,
    MULTI_WORKSPACE_TABLES,
    REQUIRED_TABLES,
    check_supabase_capabilities,
    check_existing_workspace_attachment,
    check_supabase,
    check_supabase_schema,
    main,
    _columns_for,
)


class CheckClient:
    def __init__(
        self,
        *,
        workspace_exists: bool = True,
        capabilities: dict[str, object] | None = None,
    ) -> None:
        self.workspace_exists = workspace_exists
        self.calls = []
        self.rpc_calls = []
        self.capabilities = capabilities or {
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

    def select(self, table: str, **kwargs):
        self.calls.append((table, kwargs))
        if table == "workspaces":
            return (
                [{"id": "workspace-1", "name": "Main workshop"}]
                if self.workspace_exists
                else []
            )
        return []

    def rpc(self, function: str, arguments=None):
        self.rpc_calls.append((function, arguments))
        return self.capabilities


def test_supabase_check_verifies_workspace_and_every_contract_table() -> None:
    client = CheckClient()

    result = check_supabase(client, workspace_id="workspace-1")

    assert result == {
        "schema": "magpie.supabase_check.v2",
        "ready": True,
        "workspace": {"id": "workspace-1", "name": "Main workshop"},
        "tables": list(REQUIRED_TABLES),
        "table_count": len(REQUIRED_TABLES),
        "capabilities": {
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
        },
    }
    assert client.rpc_calls == [("magpie_schema_capabilities", None)]
    assert [table for table, _ in client.calls] == ["workspaces", *REQUIRED_TABLES]
    for table, kwargs in client.calls[1:]:
        assert kwargs["filters"] == (
            None if table == "item_aliases" else {"workspace_id": "eq.workspace-1"}
        )
        assert kwargs["limit"] == 1
        assert kwargs["columns"] == _columns_for(table)


def test_supabase_check_rejects_missing_active_guidance_state_table() -> None:
    class MissingGuidanceClient(CheckClient):
        def select(self, table: str, **kwargs):
            if table == "active_guidance_states":
                raise RuntimeError("relation active_guidance_states does not exist")
            return super().select(table, **kwargs)

    with pytest.raises(RuntimeError, match="active_guidance_states"):
        check_supabase(MissingGuidanceClient(), workspace_id="workspace-1")


def test_supabase_check_fails_for_inaccessible_workspace() -> None:
    with pytest.raises(RuntimeError, match="workspace-1"):
        check_supabase(CheckClient(workspace_exists=False), workspace_id="workspace-1")


def test_supabase_check_rejects_missing_database_capability() -> None:
    client = CheckClient(
        capabilities={
            "schema": "magpie.supabase_capabilities.v1",
            "features": {
                "atomic_location_creation": True,
                "atomic_location_lifecycle": False,
                "atomic_hardware_bindings": True,
                "atomic_inventory_import": True,
                "archive_group_provenance": True,
                "guided_location_audits": True,
                "location_content_revisions": True,
            },
        }
    )

    with pytest.raises(RuntimeError, match="atomic_location_lifecycle"):
        check_supabase_capabilities(client)


class AttachmentClient(CheckClient):
    def __init__(self, *, matching_owner: bool = True) -> None:
        super().__init__()
        self.matching_owner = matching_owner

    def select(self, table: str, **kwargs):
        self.calls.append((table, kwargs))
        if table == "workspaces":
            columns = kwargs.get("columns")
            return [
                {
                    "id": "workspace-1",
                    "name": "Main workshop",
                    **({"owner_id": "user-owner"} if "owner_id" in columns else {}),
                }
            ]
        if table == "workspace_members" and kwargs.get("limit") == 10_000:
            return [
                {
                    "user_id": (
                        "user-owner" if self.matching_owner else "different-user"
                    ),
                    "role": "owner",
                },
                {"user_id": "user-member", "role": "member"},
            ]
        if table in ATTACHMENT_COUNT_TABLES and kwargs.get("limit") == 10_000:
            return [{"id": f"{table}-1"}, {"id": f"{table}-2"}]
        return []


def test_existing_workspace_attachment_is_read_only_and_pii_free() -> None:
    client = AttachmentClient()

    result = check_existing_workspace_attachment(
        client,
        workspace_id="workspace-1",
        include_edge=True,
    )

    assert result["schema"] == "magpie.supabase_attachment_check.v1"
    assert result["ready"] is True
    assert result["workspace"] == {
        "id": "workspace-1",
        "name": "Main workshop",
    }
    assert result["membership"] == {
        "total": 2,
        "roles": {"member": 1, "owner": 1},
        "owner_matches_workspace": True,
    }
    assert result["data_counts"] == {
        table: 2 for table in ATTACHMENT_COUNT_TABLES
    }
    assert result["tables"] == [*REQUIRED_TABLES, *EDGE_TABLES]
    assert "user-owner" not in json.dumps(result)
    assert "user-member" not in json.dumps(result)
    assert all(
        kwargs.get("method") is None
        for _table, kwargs in client.calls
    )


def test_existing_workspace_attachment_requires_matching_owner_membership() -> None:
    with pytest.raises(RuntimeError, match="matching owner membership"):
        check_existing_workspace_attachment(
            AttachmentClient(matching_owner=False),
            workspace_id="workspace-1",
        )


def test_supabase_schema_check_supports_empty_multi_workspace_service() -> None:
    client = CheckClient(workspace_exists=False)

    result = check_supabase_schema(client)

    assert result == {
        "schema": "magpie.supabase_check.v2",
        "ready": True,
        "mode": "multi_workspace",
        "tables": list(MULTI_WORKSPACE_TABLES),
        "table_count": len(MULTI_WORKSPACE_TABLES),
        "capabilities": {
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
        },
    }
    assert [table for table, _ in client.calls] == list(MULTI_WORKSPACE_TABLES)
    for table, kwargs in client.calls:
        assert kwargs == {
            "columns": _columns_for(table),
            "limit": 1,
        }


def test_supabase_schema_check_requires_edge_tables_for_hardware_bridge() -> None:
    client = CheckClient(workspace_exists=False)

    result = check_supabase_schema(client, include_edge=True)

    assert result["tables"] == [*MULTI_WORKSPACE_TABLES, *EDGE_TABLES]
    assert result["table_count"] == len(MULTI_WORKSPACE_TABLES) + len(EDGE_TABLES)
    edge_calls = {table: kwargs for table, kwargs in client.calls if table in EDGE_TABLES}
    assert edge_calls["edge_devices"]["columns"] == (
        "workspace_id,device_id,credential_hash,credential_scopes,"
        "pairing_hash,pairing_scopes,pairing_expires_at"
    )
    assert edge_calls["edge_commands"]["columns"] == "*"


def test_workspace_edge_config_enables_edge_schema_preflight(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "pixel_count": 0,
                "backend": "none",
                "storage": {
                    "backend": "supabase",
                    "supabase_url": "https://project.supabase.co",
                    "workspace_id": "",
                },
                "auth": {
                    "mode": "supabase",
                    "supabase_url": "https://project.supabase.co",
                    "workspace_id": "",
                    "multi_workspace": True,
                },
                "edge": {"mode": "workspace", "target_device_id": ""},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")
    checked = []

    class RecordingClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def select(self, table, **_kwargs):
            checked.append(table)
            return []

        def rpc(self, function, arguments=None):
            assert function == "magpie_schema_capabilities"
            assert arguments is None
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

    monkeypatch.setattr(
        "searchable_storage_led.supabase_check.SupabaseRestClient",
        RecordingClient,
    )
    monkeypatch.setattr(
        "sys.argv", ["supabase-check", "--config", str(config_path)]
    )

    main()

    assert checked[-2:] == list(EDGE_TABLES)
