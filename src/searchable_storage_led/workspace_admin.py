from __future__ import annotations

import os
import threading
from typing import Callable, Mapping, Protocol

from .auth import AuthIdentity
from .config import AuthConfig
from .supabase_client import SupabaseRestClient
from .workspace_provision import (
    InvitedUser,
    SupabaseAuthAdminClient,
    provision_workspace,
)


WORKSPACE_ROLES = frozenset({"owner", "admin", "member", "viewer"})
ASSIGNABLE_ROLES = frozenset({"admin", "member", "viewer"})


class WorkspaceMemberService(Protocol):
    def workspace(self) -> dict[str, object]: ...

    def members(self) -> list[dict[str, object]]: ...

    def member(self, user_id: str) -> dict[str, object]: ...

    def invite(self, email: str, role: str, *, redirect_url: str = "") -> dict[str, object]: ...

    def set_role(self, user_id: str, role: str) -> dict[str, object]: ...

    def remove(self, user_id: str) -> dict[str, object]: ...


class WorkspaceCatalogService(Protocol):
    def workspaces(self, user_id: str) -> list[dict[str, object]]: ...

    def create(self, user_id: str, name: str) -> dict[str, object]: ...

    def rename(
        self, user_id: str, workspace_id: str, name: str
    ) -> dict[str, object]: ...

    def delete(
        self, user_id: str, workspace_id: str, confirmation: str
    ) -> dict[str, object]: ...

    def delete_account(self, user_id: str) -> dict[str, object]: ...


class SupabaseWorkspaceCatalogService:
    """List and create only the workspaces owned or joined by one user."""

    def __init__(
        self,
        *,
        service_client: SupabaseRestClient,
        photo_bucket: str = "magpie-photos",
        auth_admin: SupabaseAuthAdminClient | None = None,
    ) -> None:
        self.service_client = service_client
        self.photo_bucket = photo_bucket.strip()
        self.auth_admin = auth_admin
        if not self.photo_bucket:
            raise ValueError("Supabase photo bucket is required")

    def workspaces(self, user_id: str) -> list[dict[str, object]]:
        memberships = self.service_client.select(
            "workspace_members",
            columns="workspace_id,user_id,role,created_at",
            filters={"user_id": f"eq.{user_id}"},
            order="created_at.asc",
        )
        membership_pairs = [
            (row, str(row.get("workspace_id") or ""))
            for row in memberships
            if str(row.get("workspace_id") or "")
        ]
        if not membership_pairs:
            return []
        workspace_ids = [workspace_id for _, workspace_id in membership_pairs]
        rows = self.service_client.select(
            "workspaces",
            columns="id,name,owner_id,created_at,updated_at",
            filters={"id": f"in.({','.join(workspace_ids)})"},
            order="created_at.asc",
        )
        by_id = {str(row.get("id") or ""): row for row in rows}
        return [
            {
                **by_id[workspace_id],
                "role": str(membership.get("role") or "member"),
            }
            for membership, workspace_id in membership_pairs
            if workspace_id in by_id
        ]

    def create(self, user_id: str, name: str) -> dict[str, object]:
        return provision_workspace(
            self.service_client,
            owner_user_id=user_id,
            workspace_name=name,
        )

    def rename(
        self, user_id: str, workspace_id: str, name: str
    ) -> dict[str, object]:
        clean_name = " ".join(name.split())
        if not clean_name:
            raise ValueError("workspace name is required")
        if len(clean_name) > 120:
            raise ValueError("workspace name must be 120 characters or fewer")
        owned = self.service_client.select(
            "workspaces",
            columns="id,name,owner_id",
            filters={
                "id": f"eq.{workspace_id}",
                "owner_id": f"eq.{user_id}",
            },
            limit=1,
        )
        if not owned:
            raise PermissionError("only the workspace owner can rename this workspace")
        duplicates = self.service_client.select(
            "workspaces",
            columns="id",
            filters={
                "owner_id": f"eq.{user_id}",
                "name": f"eq.{clean_name}",
            },
        )
        if any(str(row.get("id") or "") != workspace_id for row in duplicates):
            raise ValueError("you already own a workspace with that name")
        rows = self.service_client.update(
            "workspaces",
            {"name": clean_name},
            filters={
                "id": f"eq.{workspace_id}",
                "owner_id": f"eq.{user_id}",
            },
            return_rows=True,
        )
        if not rows:
            raise RuntimeError("workspace was not renamed")
        return rows[0]

    def delete(
        self, user_id: str, workspace_id: str, confirmation: str
    ) -> dict[str, object]:
        rows = self.service_client.select(
            "workspaces",
            columns="id,name,owner_id",
            filters={
                "id": f"eq.{workspace_id}",
                "owner_id": f"eq.{user_id}",
            },
            limit=1,
        )
        if not rows:
            raise PermissionError("only the workspace owner can delete this workspace")
        workspace = rows[0]
        name = str(workspace.get("name") or "")
        if confirmation.strip() != name:
            raise ValueError("workspace name confirmation does not match")

        memberships = self.service_client.select(
            "workspace_members",
            columns="user_id",
            filters={"workspace_id": f"eq.{workspace_id}"},
        )
        photos = self.service_client.select(
            "photos",
            columns="storage_path",
            filters={"workspace_id": f"eq.{workspace_id}"},
        )
        paths = [
            str(photo.get("storage_path") or "")
            for photo in photos
            if str(photo.get("storage_path") or "")
        ]
        expected_prefix = f"{workspace_id}/"
        if any(not path.startswith(expected_prefix) for path in paths):
            raise RuntimeError("workspace contains an invalid photo storage path")
        for offset in range(0, len(paths), 100):
            self.service_client.delete_objects(
                bucket=self.photo_bucket,
                paths=paths[offset : offset + 100],
            )
        deleted = self.service_client.delete(
            "workspaces",
            filters={
                "id": f"eq.{workspace_id}",
                "owner_id": f"eq.{user_id}",
            },
            return_rows=True,
        )
        if not deleted:
            raise RuntimeError("workspace was not deleted")
        return {
            "workspace": workspace,
            "member_user_ids": [
                str(member.get("user_id") or "")
                for member in memberships
                if str(member.get("user_id") or "")
            ],
            "deleted_photo_objects": len(paths),
        }

    def delete_account(self, user_id: str) -> dict[str, object]:
        memberships = self.service_client.select(
            "workspace_members",
            columns="workspace_id",
            filters={"user_id": f"eq.{user_id}"},
            limit=1,
        )
        if memberships:
            raise ValueError("leave or delete every workspace before deleting your account")
        if self.auth_admin is None:
            raise RuntimeError("account deletion is not configured")
        self.auth_admin.delete_user(user_id)
        return {"user_id": user_id}

    def close(self) -> None:
        if self.auth_admin is not None:
            self.auth_admin.close()


class WorkspaceMemberServiceRouter:
    def __init__(
        self,
        factory: Callable[[str], WorkspaceMemberService],
        *,
        default_workspace_id: str = "",
    ) -> None:
        self._factory = factory
        self._default_workspace_id = default_workspace_id.strip()
        self._services: dict[str, WorkspaceMemberService] = {}
        self._lock = threading.RLock()

    def for_workspace(self, workspace_id: str) -> WorkspaceMemberService:
        clean_workspace = workspace_id.strip() or self._default_workspace_id
        if not clean_workspace:
            raise ValueError("workspace member service requires a workspace id")
        with self._lock:
            service = self._services.get(clean_workspace)
            if service is None:
                service = self._factory(clean_workspace)
                self._services[clean_workspace] = service
            return service

    def close(self) -> None:
        seen: set[int] = set()
        for service in self._services.values():
            if id(service) in seen:
                continue
            seen.add(id(service))
            close = getattr(service, "close", None)
            if callable(close):
                close()


class SupabaseWorkspaceMemberService:
    def __init__(
        self,
        *,
        service_client: SupabaseRestClient,
        auth_admin: SupabaseAuthAdminClient,
        workspace_id: str,
    ) -> None:
        self.service_client = service_client
        self.auth_admin = auth_admin
        self.workspace_id = workspace_id

    def workspace(self) -> dict[str, object]:
        rows = self.service_client.select(
            "workspaces",
            columns="id,name,owner_id,created_at,updated_at",
            filters={"id": f"eq.{self.workspace_id}"},
            limit=1,
        )
        if not rows:
            raise RuntimeError("configured workspace does not exist")
        return rows[0]

    def members(self) -> list[dict[str, object]]:
        rows = self.service_client.select(
            "workspace_members",
            columns="workspace_id,user_id,role,created_at",
            filters={"workspace_id": f"eq.{self.workspace_id}"},
            order="created_at.asc",
        )
        users = {user.user_id: user for user in self.auth_admin.list_users()}
        return [
            {
                **row,
                "email": users.get(str(row.get("user_id") or ""), InvitedUser("", "")).email,
            }
            for row in rows
        ]

    def invite(
        self,
        email: str,
        role: str,
        *,
        redirect_url: str = "",
    ) -> dict[str, object]:
        clean_role = _assignable_role(role)
        clean_email = email.strip().lower()
        user = self.auth_admin.find_user_by_email(clean_email)
        invited = user is None
        if user is None:
            user = self.auth_admin.invite_user_by_email(
                clean_email,
                redirect_url=redirect_url,
            )
        rows = self.service_client.upsert(
            "workspace_members",
            {
                "workspace_id": self.workspace_id,
                "user_id": user.user_id,
                "role": clean_role,
            },
            on_conflict="workspace_id,user_id",
            return_rows=True,
        )
        if not rows:
            raise RuntimeError("Supabase did not return the workspace membership")
        return {**rows[0], "email": user.email, "invited": invited}

    def member(self, user_id: str) -> dict[str, object]:
        return self._membership(user_id)

    def set_role(self, user_id: str, role: str) -> dict[str, object]:
        clean_role = _assignable_role(role)
        current = self._membership(user_id)
        if current.get("role") == "owner":
            raise ValueError("workspace ownership cannot be changed here")
        rows = self.service_client.update(
            "workspace_members",
            {"role": clean_role},
            filters={
                "workspace_id": f"eq.{self.workspace_id}",
                "user_id": f"eq.{user_id}",
            },
            return_rows=True,
        )
        if not rows:
            raise RuntimeError("workspace membership was not updated")
        return rows[0]

    def remove(self, user_id: str) -> dict[str, object]:
        current = self._membership(user_id)
        if current.get("role") == "owner":
            raise ValueError("workspace owner cannot be removed")
        rows = self.service_client.delete(
            "workspace_members",
            filters={
                "workspace_id": f"eq.{self.workspace_id}",
                "user_id": f"eq.{user_id}",
            },
            return_rows=True,
        )
        if not rows:
            raise RuntimeError("workspace membership was not removed")
        return rows[0]

    def _membership(self, user_id: str) -> dict[str, object]:
        rows = self.service_client.select(
            "workspace_members",
            columns="workspace_id,user_id,role,created_at",
            filters={
                "workspace_id": f"eq.{self.workspace_id}",
                "user_id": f"eq.{user_id}",
            },
            limit=1,
        )
        if not rows:
            raise KeyError("workspace member not found")
        return rows[0]

    def close(self) -> None:
        self.auth_admin.close()


def can_manage_members(identity: AuthIdentity, *, requested_role: str = "") -> bool:
    if identity.role == "owner":
        return True
    if identity.role != "admin":
        return False
    return not requested_role or requested_role in {"member", "viewer"}


def create_workspace_member_service(
    config: AuthConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> SupabaseWorkspaceMemberService:
    values = environ if environ is not None else os.environ
    secret_key = values.get(config.service_key_env, "").strip()
    if not secret_key:
        raise ValueError(
            f"Supabase service key environment variable is not set: {config.service_key_env}"
        )
    return SupabaseWorkspaceMemberService(
        service_client=SupabaseRestClient(url=config.supabase_url, api_key=secret_key),
        auth_admin=SupabaseAuthAdminClient(
            url=config.supabase_url,
            secret_key=secret_key,
        ),
        workspace_id=config.workspace_id,
    )


def create_workspace_catalog_service(
    config: AuthConfig,
    *,
    photo_bucket: str = "magpie-photos",
    environ: Mapping[str, str] | None = None,
) -> SupabaseWorkspaceCatalogService:
    values = environ if environ is not None else os.environ
    secret_key = values.get(config.service_key_env, "").strip()
    if not secret_key:
        raise ValueError(
            f"Supabase service key environment variable is not set: {config.service_key_env}"
        )
    return SupabaseWorkspaceCatalogService(
        service_client=SupabaseRestClient(url=config.supabase_url, api_key=secret_key),
        photo_bucket=photo_bucket,
        auth_admin=SupabaseAuthAdminClient(
            url=config.supabase_url,
            secret_key=secret_key,
        ),
    )


def create_workspace_member_service_router(
    config: AuthConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> WorkspaceMemberServiceRouter:
    values = environ if environ is not None else os.environ
    secret_key = values.get(config.service_key_env, "").strip()
    if not secret_key:
        raise ValueError(
            f"Supabase service key environment variable is not set: {config.service_key_env}"
        )
    service_client = SupabaseRestClient(url=config.supabase_url, api_key=secret_key)

    def factory(workspace_id: str) -> WorkspaceMemberService:
        return SupabaseWorkspaceMemberService(
            service_client=service_client,
            auth_admin=SupabaseAuthAdminClient(
                url=config.supabase_url,
                secret_key=secret_key,
            ),
            workspace_id=workspace_id,
        )

    return WorkspaceMemberServiceRouter(
        factory,
        default_workspace_id=config.workspace_id,
    )


def _assignable_role(role: str) -> str:
    clean_role = role.strip().lower()
    if clean_role not in ASSIGNABLE_ROLES:
        raise ValueError("role must be admin, member, or viewer")
    return clean_role
