import json

import httpx
import pytest

from searchable_storage_led.workspace_provision import (
    SupabaseAuthAdminClient,
    hosted_config,
    provision_workspace,
)


OWNER_ID = "11111111-1111-4111-8111-111111111111"


class ProvisionClient:
    def __init__(self, *, existing=False, owner_membership=True) -> None:
        self.existing = existing
        self.owner_membership = owner_membership
        self.inserted = []

    def select(self, table, **kwargs):
        if table == "workspaces":
            return (
                [{"id": "workspace-1", "owner_id": OWNER_ID, "name": "My Home"}]
                if self.existing
                else []
            )
        if table == "workspace_members" and self.owner_membership:
            return [
                {"workspace_id": "workspace-1", "user_id": OWNER_ID, "role": "owner"}
            ]
        return []

    def insert(self, table, values, **kwargs):
        self.inserted.append((table, values, kwargs))
        return [{"id": "workspace-1", **values}]


def test_provision_workspace_creates_and_verifies_owner_membership() -> None:
    client = ProvisionClient()

    result = provision_workspace(
        client,
        owner_user_id=OWNER_ID,
        workspace_name="  My   Home ",
    )

    assert result["created"] is True
    assert result["workspace"] == {"id": "workspace-1", "name": "My Home"}
    assert client.inserted[0][0] == "workspaces"
    assert client.inserted[0][1] == {"owner_id": OWNER_ID, "name": "My Home"}


def test_provision_workspace_is_idempotent_for_owner_and_name() -> None:
    client = ProvisionClient(existing=True)

    result = provision_workspace(
        client,
        owner_user_id=OWNER_ID,
        workspace_name="My Home",
    )

    assert result["created"] is False
    assert client.inserted == []


def test_provision_workspace_requires_owner_membership_trigger() -> None:
    with pytest.raises(RuntimeError, match="owner membership"):
        provision_workspace(
            ProvisionClient(owner_membership=False),
            owner_user_id=OWNER_ID,
            workspace_name="My Home",
        )


def test_auth_admin_invites_owner_with_trusted_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/v1/invite"
        assert request.url.params["redirect_to"] == "https://magpie.example/"
        assert request.headers["apikey"] == "secret"
        assert request.headers["authorization"] == "Bearer secret"
        assert request.content == b'{"email":"owner@example.com"}'
        return httpx.Response(200, json={"id": OWNER_ID, "email": "owner@example.com"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        admin = SupabaseAuthAdminClient(
            url="https://project.supabase.co",
            secret_key="secret",
            client=client,
        )
        invited = admin.invite_user_by_email(
            "Owner@Example.com",
            redirect_url="https://magpie.example/",
        )

    assert invited.user_id == OWNER_ID
    assert invited.email == "owner@example.com"


def test_auth_admin_surfaces_invite_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"msg": "User already registered"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        admin = SupabaseAuthAdminClient(
            url="https://project.supabase.co",
            secret_key="secret",
            client=client,
        )
        with pytest.raises(RuntimeError, match="User already registered"):
            admin.invite_user_by_email("owner@example.com")


def test_auth_admin_finds_existing_user_case_insensitively() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/v1/admin/users"
        assert request.url.params["page"] == "1"
        return httpx.Response(
            200,
            json={"users": [{"id": OWNER_ID, "email": "Owner@Example.com"}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        admin = SupabaseAuthAdminClient(
            url="https://project.supabase.co",
            secret_key="secret",
            client=client,
        )
        user = admin.find_user_by_email("owner@example.com")

    assert user is not None
    assert user.user_id == OWNER_ID
    assert user.email == "owner@example.com"


def test_auth_admin_deletes_user_with_trusted_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == f"/auth/v1/admin/users/{OWNER_ID}"
        assert request.headers["apikey"] == "secret"
        assert request.headers["authorization"] == "Bearer secret"
        assert json.loads(request.content) == {"should_soft_delete": False}
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        admin = SupabaseAuthAdminClient(
            url="https://project.supabase.co",
            secret_key="secret",
            client=client,
        )
        admin.delete_user(OWNER_ID)


def test_auth_admin_surfaces_delete_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "delete failed"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        admin = SupabaseAuthAdminClient(
            url="https://project.supabase.co",
            secret_key="secret",
            client=client,
        )
        with pytest.raises(RuntimeError, match="delete failed"):
            admin.delete_user(OWNER_ID)


def test_hosted_config_uses_one_workspace_for_auth_and_storage() -> None:
    config = hosted_config(
        supabase_url="https://project.supabase.co/",
        workspace_id="workspace-1",
    )

    assert config["storage"]["workspace_id"] == "workspace-1"
    assert config["auth"]["workspace_id"] == "workspace-1"
    assert config["storage"]["supabase_url"] == config["auth"]["supabase_url"]
