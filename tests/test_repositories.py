import json

import pytest
from fastapi.testclient import TestClient

from searchable_storage_led.api import create_app
from searchable_storage_led.config import LedConfig
from searchable_storage_led.config import StorageConfig
from searchable_storage_led.repositories import (
    INVENTORY_REPOSITORY_METHODS,
    StorageRepositories,
    WorkspaceRepositoryRouter,
    missing_repository_methods,
)
from searchable_storage_led.supabase_repositories import SupabaseInventoryRepository


def _config(tmp_path, *, database_name: str = "configured.db") -> LedConfig:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "name": "repository-test",
                "columns": 1,
                "rows_bottom_to_top": [
                    {"size": "small", "bin_count": 1, "spans": [1]},
                ],
            }
        ),
        encoding="utf-8",
    )
    return LedConfig(
        pixel_count=1,
        backend="wled",
        wled_url="http://127.0.0.1:1",
        layout_path=str(layout_path),
        database_path=str(tmp_path / database_name),
    )


def test_sqlite_repository_bundle_satisfies_complete_contract(tmp_path) -> None:
    repositories = StorageRepositories.sqlite(tmp_path / "inventory.db")

    assert repositories.info() == {
        "backend": "sqlite",
        "workspace_id": "local",
    }
    assert repositories.health_probe() == {
        "ok": True,
        "backend": "sqlite",
        "workspace_id": "local",
    }


def test_repository_bundle_rejects_incomplete_adapters() -> None:
    with pytest.raises(TypeError, match="repository contract is incomplete") as exc:
        StorageRepositories(
            inventory=object(),
            locations=object(),
            backend="supabase",
            workspace_id="workspace-1",
        )

    assert "inventory: add_photo" in str(exc.value)
    assert "locations: all" in str(exc.value)


def test_app_uses_injected_repositories_and_reports_backend(tmp_path) -> None:
    cfg = _config(tmp_path)
    sqlite = StorageRepositories.sqlite(tmp_path / "injected.db")
    repositories = StorageRepositories(
        inventory=sqlite.inventory,
        locations=sqlite.locations,
        backend="test-adapter",
        workspace_id="workspace-123",
    )
    item_id = repositories.inventory.upsert_item(name="Injected item")
    repositories.inventory.put_inventory(
        item_id=item_id,
        bin_id="B001",
        quantity=2,
        unit="each",
    )

    client = TestClient(create_app(cfg, repositories=repositories))

    inventory = client.get("/inventory").json()["items"]
    health = client.get("/health").json()
    capabilities = client.get("/capabilities").json()

    assert [item["name"] for item in inventory] == ["Injected item"]
    assert health["storage"] == {
        "backend": "test-adapter",
        "workspace_id": "workspace-123",
    }
    assert capabilities["storage"] == health["storage"]
    assert capabilities["features"]["storage_repository_contract"] is True
    assert not (tmp_path / "configured.db").exists()


def test_supabase_inventory_repository_satisfies_complete_contract() -> None:
    repository = SupabaseInventoryRepository(object(), workspace_id="workspace-1")

    assert missing_repository_methods(repository, INVENTORY_REPOSITORY_METHODS) == []


def test_repository_factory_builds_supabase_bundle_without_exposing_key() -> None:
    repositories = StorageRepositories.from_config(
        StorageConfig(
            backend="supabase",
            supabase_url="https://project.supabase.co",
            api_key_env="MAGPIE_SUPABASE_KEY",
            workspace_id="workspace-1",
        ),
        sqlite_path="unused.db",
        environ={"MAGPIE_SUPABASE_KEY": "service-role-secret"},
    )

    assert repositories.info() == {
        "backend": "supabase",
        "workspace_id": "workspace-1",
    }
    assert repositories.inventory.client.api_key == "service-role-secret"
    assert repositories.locations.client is repositories.inventory.client


def test_repository_factory_rejects_missing_supabase_key() -> None:
    with pytest.raises(ValueError, match="MAGPIE_SUPABASE_KEY"):
        StorageRepositories.from_config(
            StorageConfig(
                backend="supabase",
                supabase_url="https://project.supabase.co",
                api_key_env="MAGPIE_SUPABASE_KEY",
                workspace_id="workspace-1",
            ),
            sqlite_path="unused.db",
            environ={},
        )


def test_workspace_repository_router_isolates_request_contexts(tmp_path) -> None:
    created = []

    def factory(workspace_id: str) -> StorageRepositories:
        sqlite = StorageRepositories.sqlite(tmp_path / f"{workspace_id}.db")
        bundle = StorageRepositories(
            inventory=sqlite.inventory,
            locations=sqlite.locations,
            backend="test",
            workspace_id=workspace_id,
        )
        created.append(workspace_id)
        return bundle

    router = WorkspaceRepositoryRouter(factory, backend="test")

    first_token = router.bind("workspace-1")
    first_item_id = router.inventory.upsert_item(name="Only in one")
    router.inventory.put_inventory(
        item_id=first_item_id,
        bin_id="B001",
        quantity=1,
        unit="each",
    )
    assert [item.name for item in router.inventory.list_inventory()] == [
        "Only in one"
    ]
    router.reset(first_token)

    second_token = router.bind("workspace-2")
    assert router.inventory.list_inventory() == []
    second_item_id = router.inventory.upsert_item(name="Only in two")
    router.inventory.put_inventory(
        item_id=second_item_id,
        bin_id="B002",
        quantity=1,
        unit="each",
    )
    router.reset(second_token)

    first_token = router.bind("workspace-1")
    assert [item.name for item in router.inventory.list_inventory()] == [
        "Only in one"
    ]
    assert router.info() == {"backend": "test", "workspace_id": "workspace-1"}
    router.reset(first_token)

    assert created == ["workspace-1", "workspace-2"]
    assert router.info() == {
        "backend": "test",
        "workspace_id": "request-scoped",
    }


def test_dynamic_repository_readiness_uses_an_isolated_probe_workspace(tmp_path) -> None:
    created = []

    def factory(workspace_id: str) -> StorageRepositories:
        created.append(workspace_id)
        sqlite = StorageRepositories.sqlite(tmp_path / f"{workspace_id}.db")
        return StorageRepositories(
            inventory=sqlite.inventory,
            locations=sqlite.locations,
            backend="test",
            workspace_id=workspace_id,
        )

    router = WorkspaceRepositoryRouter(factory, backend="test")

    assert router.health_probe() == {
        "ok": True,
        "backend": "test",
        "workspace_id": "request-scoped",
    }
    assert created == ["00000000-0000-0000-0000-000000000000"]
    assert router.info() == {
        "backend": "test",
        "workspace_id": "request-scoped",
    }


def test_fixed_repository_router_preserves_existing_default_bundle(tmp_path) -> None:
    sqlite = StorageRepositories.sqlite(tmp_path / "fixed.db")
    bundle = StorageRepositories(
        inventory=sqlite.inventory,
        locations=sqlite.locations,
        backend="test",
        workspace_id="workspace-1",
    )
    router = WorkspaceRepositoryRouter.fixed(bundle)

    item_id = router.inventory.upsert_item(name="Default item")
    router.inventory.put_inventory(
        item_id=item_id,
        bin_id="B001",
        quantity=1,
        unit="each",
    )

    assert router.current() is bundle
    assert router.info() == {"backend": "test", "workspace_id": "workspace-1"}


def test_dynamic_supabase_router_reuses_transport_but_isolates_workspace_metering() -> None:
    router = WorkspaceRepositoryRouter.from_config(
        StorageConfig(
            backend="supabase",
            supabase_url="https://project.supabase.co",
            api_key_env="MAGPIE_SUPABASE_KEY",
            workspace_id="",
        ),
        sqlite_path="unused.db",
        environ={"MAGPIE_SUPABASE_KEY": "service-role-secret"},
    )

    first = router.for_workspace("workspace-1")
    second = router.for_workspace("workspace-2")

    assert router.dynamic is True
    assert first.workspace_id == "workspace-1"
    assert second.workspace_id == "workspace-2"
    assert first.inventory.client is not second.inventory.client
    assert first.inventory.client.transport is second.inventory.client.transport
    assert first.locations.client is first.inventory.client
    assert first.inventory.client.usage_snapshot()["logical_requests"] == 0
    assert second.inventory.client.usage_snapshot()["logical_requests"] == 0
