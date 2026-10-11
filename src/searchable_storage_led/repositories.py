from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
import os
from pathlib import Path
import threading
from typing import Callable, Generic, Mapping, Protocol, TypeVar

from .config import StorageConfig
from .locations import LocationStore
from .store import InventoryStore
from .supabase_client import SupabaseRestClient
from .supabase_repositories import (
    SupabaseInventoryRepository,
    SupabaseLocationRepository,
)


# This is the storage surface exercised outside the concrete SQLite classes.
# An alternate backend must satisfy the whole contract before app startup.
INVENTORY_REPOSITORY_METHODS = frozenset(
    {
        "add_photo",
        "apply_location_audit",
        "approve_example",
        "archive_item",
        "assistant_interaction",
        "assistant_interaction_by_client_request_id",
        "assistant_interactions_by_ids",
        "assistant_interactions_after",
        "attach_photo",
        "attach_photo_to_location",
        "bin_inventory",
        "clear_bin_inventory",
        "clear_item_size",
        "claim_assistant_interaction_confirmation",
        "deactivate_rule",
        "delete_photo",
        "example",
        "example_kind_counts",
        "example_review_counts",
        "examples_for_eval",
        "exact_match",
        "find_examples",
        "item_events",
        "item_inventory",
        "item_merge_preview",
        "item_size_class",
        "items_in_location",
        "items_in_locations",
        "items_in_locations_page",
        "inventory_catalog_page",
        "import_inventory_rows_atomic",
        "latest_pending_agent_plan",
        "latest_undoable_confirmed_plan",
        "learn_alias",
        "learn_rule",
        "learn_size_rule",
        "link_review_photo",
        "list_inventory",
        "location_item_counts",
        "location_inventory_metrics",
        "location_review_counts",
        "location_photos",
        "location_photos_by_location",
        "load_active_guidance_state",
        "merge_item",
        "mark_example_promoted",
        "mark_needs_review",
        "mark_reviewed",
        "matching_rules",
        "matching_size_rules",
        "move_inventory",
        "occupied_bin_ids",
        "observe_confirmed_home",
        "photo",
        "photo_content",
        "put_inventory",
        "recent_voice_attempts",
        "recent_voice_command_attempts",
        "record_assistant_interaction",
        "record_example",
        "record_voice_attempt",
        "reject_example",
        "restore_item_size_metadata",
        "restore_item_location_metadata",
        "restore_item",
        "resolve_item_id",
        "remove_alias",
        "remove_inventory",
        "review_queue",
        "rule",
        "rule_conflicts",
        "rules",
        "set_rule_active",
        "search",
        "save_active_guidance_state",
        "set_assistant_interaction_status",
        "set_photo_target",
        "size_rule",
        "size_rules",
        "suggest_home",
        "supersede_pending_agent_plans",
        "update_assistant_interaction_response",
        "update_item",
        "update_item_home",
        "update_item_size",
        "update_rule",
        "update_size_rule",
        "undo_confirmed_home_observation",
        "upsert_item",
    }
)

LOCATION_REPOSITORY_METHODS = frozenset(
    {
        "all",
        "ancestors",
        "archive_subtree",
        "children",
        "count",
        "create_many",
        "delete_subtree",
        "events",
        "get",
        "get_any",
        "health_probe",
        "hardware_bindings",
        "hardware_bindings_by_location",
        "has_event",
        "invalidate_cache",
        "latest_event",
        "latest_events_by_location",
        "move",
        "nearest_lit",
        "record_event",
        "restore_subtree",
        "set_hardware_binding",
        "set_hardware_bindings",
        "subtree_ids",
        "upsert",
    }
)


class InventoryRepository(Protocol):
    """Structural marker for the inventory side of Magpie storage."""


class LocationRepository(Protocol):
    """Structural marker for the location/hardware side of Magpie storage."""


def missing_repository_methods(repository: object, required: frozenset[str]) -> list[str]:
    return sorted(
        method
        for method in required
        if not callable(getattr(repository, method, None))
    )


@dataclass(frozen=True)
class StorageRepositories:
    inventory: InventoryRepository
    locations: LocationRepository
    backend: str = "sqlite"
    workspace_id: str = "local"

    def __post_init__(self) -> None:
        missing_inventory = missing_repository_methods(
            self.inventory,
            INVENTORY_REPOSITORY_METHODS,
        )
        missing_locations = missing_repository_methods(
            self.locations,
            LOCATION_REPOSITORY_METHODS,
        )
        problems: list[str] = []
        if missing_inventory:
            problems.append(f"inventory: {', '.join(missing_inventory)}")
        if missing_locations:
            problems.append(f"locations: {', '.join(missing_locations)}")
        if problems:
            raise TypeError("repository contract is incomplete (" + "; ".join(problems) + ")")
        if not self.backend.strip():
            raise ValueError("repository backend is required")
        if not self.workspace_id.strip():
            raise ValueError("repository workspace_id is required")

    @classmethod
    def sqlite(cls, path: str | Path) -> "StorageRepositories":
        return cls(
            inventory=InventoryStore(path),
            locations=LocationStore(path),
            backend="sqlite",
            workspace_id="local",
        )

    @classmethod
    def from_config(
        cls,
        storage: StorageConfig,
        *,
        sqlite_path: str | Path,
        environ: Mapping[str, str] | None = None,
    ) -> "StorageRepositories":
        if storage.backend == "sqlite":
            return cls.sqlite(sqlite_path)
        values = environ if environ is not None else os.environ
        api_key = values.get(storage.api_key_env, "").strip()
        if not api_key:
            raise ValueError(
                f"Supabase API key environment variable is not set: {storage.api_key_env}"
            )
        client = SupabaseRestClient(
            url=storage.supabase_url,
            api_key=api_key,
            timeout_seconds=storage.timeout_seconds,
        )
        return cls(
            inventory=SupabaseInventoryRepository(
                client,
                workspace_id=storage.workspace_id,
                photo_bucket=storage.photo_bucket,
            ),
            locations=SupabaseLocationRepository(
                client,
                workspace_id=storage.workspace_id,
            ),
            backend="supabase",
            workspace_id=storage.workspace_id,
        )

    def info(self) -> dict[str, str]:
        return {
            "backend": self.backend,
            "workspace_id": self.workspace_id,
        }

    def health_probe(self) -> dict[str, object]:
        self.locations.health_probe()
        return {
            "ok": True,
            "backend": self.backend,
            "workspace_id": self.workspace_id,
        }


RepositoryT = TypeVar("RepositoryT")


class _RoutedRepository(Generic[RepositoryT]):
    def __init__(
        self,
        router: "WorkspaceRepositoryRouter",
        selector: Callable[[StorageRepositories], RepositoryT],
    ) -> None:
        self._router = router
        self._selector = selector

    def __getattr__(self, name: str) -> object:
        return getattr(self._selector(self._router.current()), name)


class WorkspaceRepositoryRouter:
    """Resolve one complete repository bundle from the current request workspace."""

    def __init__(
        self,
        factory: Callable[[str], StorageRepositories],
        *,
        backend: str,
        default: StorageRepositories | None = None,
    ) -> None:
        self._factory = factory
        self.backend = backend.strip()
        if not self.backend:
            raise ValueError("repository router backend is required")
        self._default_workspace_id = default.workspace_id if default is not None else ""
        self._bundles: dict[str, StorageRepositories] = {}
        if default is not None:
            self._bundles[default.workspace_id] = default
        self._lock = threading.RLock()
        self._workspace: ContextVar[str] = ContextVar(
            f"magpie_workspace_{id(self)}",
            default="",
        )
        self.inventory: InventoryRepository = _RoutedRepository(
            self,
            lambda bundle: bundle.inventory,
        )  # type: ignore[assignment]
        self.locations: LocationRepository = _RoutedRepository(
            self,
            lambda bundle: bundle.locations,
        )  # type: ignore[assignment]

    @classmethod
    def fixed(cls, bundle: StorageRepositories) -> "WorkspaceRepositoryRouter":
        return cls(
            lambda workspace_id: bundle
            if workspace_id == bundle.workspace_id
            else _raise_unknown_workspace(workspace_id),
            backend=bundle.backend,
            default=bundle,
        )

    @classmethod
    def from_config(
        cls,
        storage: StorageConfig,
        *,
        sqlite_path: str | Path,
        environ: Mapping[str, str] | None = None,
    ) -> "WorkspaceRepositoryRouter":
        if storage.backend == "sqlite":
            return cls.fixed(StorageRepositories.sqlite(sqlite_path))
        values = environ if environ is not None else os.environ
        api_key = values.get(storage.api_key_env, "").strip()
        if not api_key:
            raise ValueError(
                f"Supabase API key environment variable is not set: {storage.api_key_env}"
            )
        pooled_client = SupabaseRestClient(
            url=storage.supabase_url,
            api_key=api_key,
            timeout_seconds=storage.timeout_seconds,
        )

        def factory(workspace_id: str) -> StorageRepositories:
            # Reuse the HTTP pool without combining per-workspace traffic
            # counters exposed through account diagnostics.
            client = SupabaseRestClient(
                url=storage.supabase_url,
                api_key=api_key,
                timeout_seconds=storage.timeout_seconds,
                transport=pooled_client.transport,
            )
            return StorageRepositories(
                inventory=SupabaseInventoryRepository(
                    client,
                    workspace_id=workspace_id,
                    photo_bucket=storage.photo_bucket,
                ),
                locations=SupabaseLocationRepository(
                    client,
                    workspace_id=workspace_id,
                ),
                backend="supabase",
                workspace_id=workspace_id,
            )

        default = factory(storage.workspace_id) if storage.workspace_id else None
        return cls(factory, backend="supabase", default=default)

    def bind(self, workspace_id: str) -> Token[str]:
        clean_workspace = workspace_id.strip()
        if not clean_workspace:
            raise ValueError("request workspace_id is required")
        self.for_workspace(clean_workspace)
        return self._workspace.set(clean_workspace)

    def reset(self, token: Token[str]) -> None:
        self._workspace.reset(token)

    def for_workspace(self, workspace_id: str) -> StorageRepositories:
        clean_workspace = workspace_id.strip()
        if not clean_workspace:
            raise ValueError("workspace_id is required")
        with self._lock:
            bundle = self._bundles.get(clean_workspace)
            if bundle is None:
                bundle = self._factory(clean_workspace)
                if bundle.workspace_id != clean_workspace:
                    raise ValueError("repository factory returned the wrong workspace")
                if bundle.backend != self.backend:
                    raise ValueError("repository factory returned the wrong backend")
                self._bundles[clean_workspace] = bundle
            return bundle

    def current(self) -> StorageRepositories:
        workspace_id = self._workspace.get() or self._default_workspace_id
        if not workspace_id:
            raise RuntimeError("no Magpie workspace is bound to this request")
        return self.for_workspace(workspace_id)

    def current_workspace_id(self) -> str:
        return self._workspace.get() or self._default_workspace_id

    @property
    def workspace_id(self) -> str:
        return self.current_workspace_id() or "request-scoped"

    def info(self) -> dict[str, str]:
        workspace_id = self.current_workspace_id()
        return {
            "backend": self.backend,
            "workspace_id": workspace_id or "request-scoped",
        }

    def health_probe(self) -> dict[str, object]:
        workspace_id = self.current_workspace_id()
        if workspace_id:
            return self.for_workspace(workspace_id).health_probe()
        probe = self._factory("00000000-0000-0000-0000-000000000000")
        if probe.backend != self.backend:
            raise RuntimeError("repository readiness probe returned the wrong backend")
        result = probe.health_probe()
        return {**result, "workspace_id": "request-scoped"}

    @property
    def dynamic(self) -> bool:
        return not self._default_workspace_id


def _raise_unknown_workspace(workspace_id: str) -> StorageRepositories:
    raise ValueError(f"repository router cannot select workspace: {workspace_id}")
