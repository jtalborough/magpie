from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote
from uuid import UUID

import httpx

from .supabase_client import SupabaseRestClient


class WorkspaceAdminClient(Protocol):
    def select(self, table: str, **kwargs) -> list[dict[str, object]]: ...

    def insert(
        self, table: str, values: dict[str, object], **kwargs
    ) -> list[dict[str, object]]: ...


@dataclass(frozen=True)
class InvitedUser:
    user_id: str
    email: str


class SupabaseAuthAdminClient:
    """Minimal trusted-server client for Supabase's email invite endpoint."""

    def __init__(
        self,
        *,
        url: str,
        secret_key: str,
        client: httpx.Client | None = None,
    ) -> None:
        self.auth_url = f"{url.strip().rstrip('/')}/auth/v1"
        self.secret_key = secret_key.strip()
        if not self.secret_key:
            raise ValueError("Supabase secret key is required")
        self.client = client or httpx.Client(timeout=15)
        self._owns_client = client is None

    def invite_user_by_email(
        self,
        email: str,
        *,
        redirect_url: str = "",
    ) -> InvitedUser:
        clean_email = email.strip().lower()
        if not clean_email or "@" not in clean_email:
            raise ValueError("a valid owner email is required")
        params = {"redirect_to": redirect_url.strip()} if redirect_url.strip() else None
        response = self.client.post(
            f"{self.auth_url}/invite",
            params=params,
            headers={
                "apikey": self.secret_key,
                "Authorization": f"Bearer {self.secret_key}",
                "Content-Type": "application/json",
            },
            json={"email": clean_email},
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError("Supabase Auth returned invalid JSON") from exc
        if not response.is_success:
            detail = (
                body.get("msg") or body.get("message")
                if isinstance(body, dict)
                else ""
            )
            raise RuntimeError(
                f"Supabase could not invite the workspace owner ({response.status_code}): "
                f"{detail or 'unknown error'}"
            )
        user_id = str(body.get("id") or "") if isinstance(body, dict) else ""
        if not user_id:
            raise RuntimeError("Supabase invite response did not include a user id")
        return InvitedUser(user_id=user_id, email=str(body.get("email") or clean_email))

    def list_users(self, *, page_size: int = 1000) -> list[InvitedUser]:
        users: list[InvitedUser] = []
        page = 1
        while True:
            response = self.client.get(
                f"{self.auth_url}/admin/users",
                params={"page": page, "per_page": page_size},
                headers={
                    "apikey": self.secret_key,
                    "Authorization": f"Bearer {self.secret_key}",
                },
            )
            body = self._response_body(response, "list Auth users")
            raw_users = body.get("users") if isinstance(body, dict) else None
            if not isinstance(raw_users, list):
                raise RuntimeError("Supabase Auth users response has no users list")
            batch = [
                InvitedUser(
                    user_id=str(row.get("id") or ""),
                    email=str(row.get("email") or "").strip().lower(),
                )
                for row in raw_users
                if isinstance(row, dict) and row.get("id")
            ]
            users.extend(batch)
            if len(raw_users) < page_size:
                return users
            page += 1

    def find_user_by_email(self, email: str) -> InvitedUser | None:
        clean_email = email.strip().lower()
        return next((user for user in self.list_users() if user.email == clean_email), None)

    def delete_user(self, user_id: str) -> None:
        clean_user_id = user_id.strip()
        if not clean_user_id:
            raise ValueError("user id is required")
        response = self.client.request(
            "DELETE",
            f"{self.auth_url}/admin/users/{quote(clean_user_id, safe='')}",
            headers={
                "apikey": self.secret_key,
                "Authorization": f"Bearer {self.secret_key}",
            },
            json={"should_soft_delete": False},
        )
        if response.is_success:
            return
        try:
            body = response.json()
        except ValueError:
            body = {}
        detail = (
            body.get("msg") or body.get("message") if isinstance(body, dict) else ""
        )
        raise RuntimeError(
            f"Supabase could not delete Auth user ({response.status_code}): "
            f"{detail or 'unknown error'}"
        )

    @staticmethod
    def _response_body(response: httpx.Response, operation: str) -> object:
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Supabase could not {operation}: invalid JSON") from exc
        if not response.is_success:
            detail = (
                body.get("msg") or body.get("message")
                if isinstance(body, dict)
                else ""
            )
            raise RuntimeError(
                f"Supabase could not {operation} ({response.status_code}): "
                f"{detail or 'unknown error'}"
            )
        return body

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


def provision_workspace(
    client: WorkspaceAdminClient,
    *,
    owner_user_id: str,
    workspace_name: str,
) -> dict[str, object]:
    try:
        owner_id = str(UUID(owner_user_id.strip()))
    except (ValueError, AttributeError) as exc:
        raise ValueError("owner_user_id must be a UUID") from exc
    clean_name = " ".join(workspace_name.split())
    if not clean_name:
        raise ValueError("workspace_name is required")

    existing = client.select(
        "workspaces",
        columns="id,owner_id,name",
        filters={"owner_id": f"eq.{owner_id}", "name": f"eq.{clean_name}"},
        limit=1,
    )
    created = False
    if existing:
        workspace = existing[0]
    else:
        rows = client.insert(
            "workspaces",
            {"owner_id": owner_id, "name": clean_name},
            return_rows=True,
        )
        if not rows:
            raise RuntimeError("Supabase did not return the created workspace")
        workspace = rows[0]
        created = True

    workspace_id = str(workspace.get("id") or "")
    if not workspace_id:
        raise RuntimeError("Supabase workspace has no id")
    membership = client.select(
        "workspace_members",
        columns="workspace_id,user_id,role",
        filters={
            "workspace_id": f"eq.{workspace_id}",
            "user_id": f"eq.{owner_id}",
        },
        limit=1,
    )
    if not membership or membership[0].get("role") != "owner":
        raise RuntimeError("workspace owner membership was not created")
    return {
        "schema": "magpie.workspace_provision.v1",
        "created": created,
        "workspace": {"id": workspace_id, "name": clean_name},
        "owner": {"user_id": owner_id, "role": "owner"},
    }


def hosted_config(*, supabase_url: str, workspace_id: str) -> dict[str, object]:
    return {
        "storage": {
            "backend": "supabase",
            "supabase_url": supabase_url.rstrip("/"),
            "api_key_env": "SUPABASE_SERVICE_ROLE_KEY",
            "workspace_id": workspace_id,
            "photo_bucket": "magpie-photos",
        },
        "auth": {
            "mode": "supabase",
            "supabase_url": supabase_url.rstrip("/"),
            "publishable_key_env": "SUPABASE_PUBLISHABLE_KEY",
            "service_key_env": "SUPABASE_SERVICE_ROLE_KEY",
            "workspace_id": workspace_id,
            "cache_seconds": 30,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="searchable-storage-provision-workspace",
        description="Provision one isolated Magpie workspace and owner.",
    )
    parser.add_argument("--supabase-url", required=True)
    parser.add_argument("--workspace-name", required=True)
    owner = parser.add_mutually_exclusive_group(required=True)
    owner.add_argument("--owner-user-id")
    owner.add_argument("--invite-email")
    parser.add_argument("--redirect-url", default="")
    parser.add_argument("--secret-key-env", default="SUPABASE_SERVICE_ROLE_KEY")
    args = parser.parse_args()

    secret_key = os.environ.get(args.secret_key_env, "").strip()
    if not secret_key:
        raise SystemExit(
            f"Supabase secret key environment variable is not set: {args.secret_key_env}"
        )
    owner_user_id = args.owner_user_id
    invited_email = ""
    auth_admin = None
    if args.invite_email:
        auth_admin = SupabaseAuthAdminClient(
            url=args.supabase_url,
            secret_key=secret_key,
        )
        try:
            invited = auth_admin.invite_user_by_email(
                args.invite_email,
                redirect_url=args.redirect_url,
            )
        finally:
            auth_admin.close()
        owner_user_id = invited.user_id
        invited_email = invited.email

    client = SupabaseRestClient(url=args.supabase_url, api_key=secret_key)
    result = provision_workspace(
        client,
        owner_user_id=owner_user_id,
        workspace_name=args.workspace_name,
    )
    result["owner"]["email"] = invited_email
    result["config"] = hosted_config(
        supabase_url=args.supabase_url,
        workspace_id=result["workspace"]["id"],
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
