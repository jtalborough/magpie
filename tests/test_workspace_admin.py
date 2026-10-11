from searchable_storage_led.auth import AuthIdentity
from searchable_storage_led.workspace_admin import (
    SupabaseWorkspaceCatalogService,
    SupabaseWorkspaceMemberService,
    WorkspaceMemberServiceRouter,
    can_manage_members,
)
from searchable_storage_led.workspace_provision import InvitedUser


class MemberClient:
    def __init__(self) -> None:
        self.members = [
            {
                "workspace_id": "workspace-1",
                "user_id": "owner-1",
                "role": "owner",
                "created_at": "now",
            }
        ]
        self.upserts = []

    def select(self, table, **kwargs):
        if table == "workspaces":
            return [{"id": "workspace-1", "name": "My Home", "owner_id": "owner-1"}]
        user_filter = (kwargs.get("filters") or {}).get("user_id")
        if user_filter:
            user_id = user_filter.removeprefix("eq.")
            return [row for row in self.members if row["user_id"] == user_id]
        return list(self.members)

    def upsert(self, table, values, **kwargs):
        self.upserts.append((table, values, kwargs))
        row = {**values, "created_at": "now"}
        self.members = [m for m in self.members if m["user_id"] != values["user_id"]]
        self.members.append(row)
        return [row]

    def update(self, table, values, **kwargs):
        user_id = kwargs["filters"]["user_id"].removeprefix("eq.")
        for row in self.members:
            if row["user_id"] == user_id:
                row.update(values)
                return [row]
        return []

    def delete(self, table, **kwargs):
        user_id = kwargs["filters"]["user_id"].removeprefix("eq.")
        found = [row for row in self.members if row["user_id"] == user_id]
        self.members = [row for row in self.members if row["user_id"] != user_id]
        return found


class AuthAdmin:
    def __init__(self, existing=None) -> None:
        self.existing = existing
        self.invites = []
        self.deleted_users = []

    def list_users(self):
        return [InvitedUser("owner-1", "owner@example.com")]

    def find_user_by_email(self, email):
        return self.existing

    def invite_user_by_email(self, email, *, redirect_url=""):
        self.invites.append((email, redirect_url))
        return InvitedUser("member-1", email)

    def delete_user(self, user_id):
        self.deleted_users.append(user_id)

    def close(self):
        pass


def service(*, existing=None):
    client = MemberClient()
    auth = AuthAdmin(existing=existing)
    return SupabaseWorkspaceMemberService(
        service_client=client,
        auth_admin=auth,
        workspace_id="workspace-1",
    ), client, auth


def test_lists_workspace_members_with_auth_emails() -> None:
    members, _client, _auth = service()

    assert members.members()[0]["email"] == "owner@example.com"
    assert members.member("owner-1")["role"] == "owner"


def test_invite_is_retryable_for_an_existing_auth_user() -> None:
    existing = InvitedUser("member-1", "member@example.com")
    members, client, auth = service(existing=existing)

    result = members.invite("Member@Example.com", "viewer")

    assert result["invited"] is False
    assert auth.invites == []
    assert client.upserts[0][1]["role"] == "viewer"


def test_invite_creates_auth_user_then_membership() -> None:
    members, _client, auth = service()

    result = members.invite(
        "new@example.com",
        "member",
        redirect_url="https://magpie.example/account",
    )

    assert result["invited"] is True
    assert auth.invites == [("new@example.com", "https://magpie.example/account")]


def test_owner_membership_cannot_be_changed_or_removed() -> None:
    members, _client, _auth = service()

    for operation in (
        lambda: members.set_role("owner-1", "viewer"),
        lambda: members.remove("owner-1"),
    ):
        try:
            operation()
        except ValueError:
            pass
        else:
            raise AssertionError("owner mutation should fail")


def test_role_management_permissions() -> None:
    owner = AuthIdentity("owner", "o@example.com", "workspace-1", "owner")
    admin = AuthIdentity("admin", "a@example.com", "workspace-1", "admin")
    member = AuthIdentity("member", "m@example.com", "workspace-1", "member")

    assert can_manage_members(owner, requested_role="admin") is True
    assert can_manage_members(admin, requested_role="viewer") is True
    assert can_manage_members(admin, requested_role="admin") is False
    assert can_manage_members(member, requested_role="viewer") is False


class CatalogClient:
    def __init__(self) -> None:
        self.workspaces = [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "name": "Workshop",
                "owner_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "created_at": "1",
                "updated_at": "1",
            },
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "name": "Attic",
                "owner_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "created_at": "2",
                "updated_at": "2",
            },
        ]
        self.memberships = [
            {
                "workspace_id": self.workspaces[0]["id"],
                "user_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "role": "owner",
                "created_at": "1",
            },
            {
                "workspace_id": self.workspaces[1]["id"],
                "user_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "role": "member",
                "created_at": "2",
            },
        ]

    def select(self, table, **kwargs):
        filters = kwargs.get("filters") or {}
        if table == "workspace_members":
            user_id = str(filters.get("user_id") or "").removeprefix("eq.")
            workspace_id = str(filters.get("workspace_id") or "").removeprefix("eq.")
            rows = [
                row
                for row in self.memberships
                if (not user_id or row["user_id"] == user_id)
                and (not workspace_id or row["workspace_id"] == workspace_id)
            ]
        else:
            raw_workspace_id = str(filters.get("id") or "")
            workspace_id = (
                raw_workspace_id.removeprefix("eq.")
                if raw_workspace_id.startswith("eq.")
                else ""
            )
            owner_id = str(filters.get("owner_id") or "").removeprefix("eq.")
            name = str(filters.get("name") or "").removeprefix("eq.")
            rows = [
                row
                for row in self.workspaces
                if (not workspace_id or row["id"] == workspace_id)
                and (not owner_id or row["owner_id"] == owner_id)
                and (not name or row["name"] == name)
            ]
        limit = kwargs.get("limit")
        return rows[:limit] if limit else rows

    def insert(self, table, values, **_kwargs):
        assert table == "workspaces"
        workspace = {
            "id": "33333333-3333-3333-3333-333333333333",
            "created_at": "3",
            "updated_at": "3",
            **values,
        }
        self.workspaces.append(workspace)
        self.memberships.append(
            {
                "workspace_id": workspace["id"],
                "user_id": values["owner_id"],
                "role": "owner",
                "created_at": "3",
            }
        )
        return [workspace]

    def update(self, table, values, **kwargs):
        assert table == "workspaces"
        workspace_id = kwargs["filters"]["id"].removeprefix("eq.")
        owner_id = kwargs["filters"]["owner_id"].removeprefix("eq.")
        for workspace in self.workspaces:
            if workspace["id"] == workspace_id and workspace["owner_id"] == owner_id:
                workspace.update(values)
                return [workspace]
        return []


class WorkspaceDeletionClient:
    def __init__(self) -> None:
        self.workspace = {
            "id": "11111111-1111-1111-1111-111111111111",
            "name": "Workshop",
            "owner_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        }
        self.deleted_objects = []
        self.deleted_workspace = False

    def select(self, table, **kwargs):
        if table == "workspaces":
            filters = kwargs.get("filters") or {}
            owner = str(filters.get("owner_id") or "").removeprefix("eq.")
            workspace = str(filters.get("id") or "").removeprefix("eq.")
            return (
                [self.workspace]
                if owner == self.workspace["owner_id"]
                and workspace == self.workspace["id"]
                else []
            )
        if table == "workspace_members":
            return [{"user_id": self.workspace["owner_id"]}, {"user_id": "member-1"}]
        if table == "photos":
            return [
                {"storage_path": f"{self.workspace['id']}/one.jpg"},
                {"storage_path": f"{self.workspace['id']}/two.jpg"},
            ]
        raise AssertionError(table)

    def delete_objects(self, *, bucket, paths):
        self.deleted_objects.append((bucket, paths))

    def delete(self, table, **kwargs):
        assert table == "workspaces"
        self.deleted_workspace = True
        return [self.workspace]


class EmptyMembershipClient:
    def select(self, table, **_kwargs):
        assert table == "workspace_members"
        return []


def test_workspace_catalog_lists_only_user_memberships_with_roles() -> None:
    service = SupabaseWorkspaceCatalogService(service_client=CatalogClient())

    rows = service.workspaces("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    assert [(row["name"], row["role"]) for row in rows] == [
        ("Workshop", "owner"),
        ("Attic", "member"),
    ]


def test_workspace_catalog_creates_owner_workspace_idempotently() -> None:
    client = CatalogClient()
    service = SupabaseWorkspaceCatalogService(service_client=client)
    user_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    first = service.create(user_id, "New Home")
    second = service.create(user_id, "New Home")

    assert first["created"] is True
    assert second["created"] is False
    assert first["workspace"] == second["workspace"]
    assert first["owner"] == {"user_id": user_id, "role": "owner"}


def test_workspace_catalog_owner_can_rename_workspace() -> None:
    client = CatalogClient()
    service = SupabaseWorkspaceCatalogService(service_client=client)
    workspace = client.workspaces[0]

    renamed = service.rename(workspace["owner_id"], workspace["id"], "  Main   Shop ")

    assert renamed["name"] == "Main Shop"
    assert client.workspaces[0]["name"] == "Main Shop"


def test_workspace_catalog_rename_rejects_non_owner_and_duplicate_name() -> None:
    client = CatalogClient()
    first = client.workspaces[0]
    client.workspaces.append(
        {
            "id": "44444444-4444-4444-4444-444444444444",
            "name": "Basement",
            "owner_id": first["owner_id"],
            "created_at": "4",
            "updated_at": "4",
        }
    )
    service = SupabaseWorkspaceCatalogService(service_client=client)

    for user_id, name, error in (
        ("not-the-owner", "New name", PermissionError),
        (first["owner_id"], "Basement", ValueError),
    ):
        try:
            service.rename(user_id, first["id"], name)
        except error:
            pass
        else:
            raise AssertionError(f"{error.__name__} was not raised")


def test_workspace_catalog_deletes_owned_workspace_and_photo_objects() -> None:
    client = WorkspaceDeletionClient()
    service = SupabaseWorkspaceCatalogService(service_client=client)

    result = service.delete(
        client.workspace["owner_id"],
        client.workspace["id"],
        "Workshop",
    )

    assert client.deleted_objects == [
        (
            "magpie-photos",
            [
                f"{client.workspace['id']}/one.jpg",
                f"{client.workspace['id']}/two.jpg",
            ],
        )
    ]
    assert client.deleted_workspace is True
    assert result["member_user_ids"] == [client.workspace["owner_id"], "member-1"]
    assert result["deleted_photo_objects"] == 2


def test_workspace_catalog_requires_exact_name_and_owner_before_deletion() -> None:
    client = WorkspaceDeletionClient()
    service = SupabaseWorkspaceCatalogService(service_client=client)

    for user_id, confirmation, error in (
        (client.workspace["owner_id"], "workshop", ValueError),
        ("not-the-owner", "Workshop", PermissionError),
    ):
        try:
            service.delete(user_id, client.workspace["id"], confirmation)
        except error:
            pass
        else:
            raise AssertionError(f"{error.__name__} was not raised")

    assert client.deleted_objects == []
    assert client.deleted_workspace is False


def test_workspace_catalog_deletes_account_only_after_all_memberships_are_gone() -> None:
    auth = AuthAdmin()
    service = SupabaseWorkspaceCatalogService(
        service_client=EmptyMembershipClient(),
        auth_admin=auth,
    )

    assert service.delete_account("user-1") == {"user_id": "user-1"}
    assert auth.deleted_users == ["user-1"]

    populated = SupabaseWorkspaceCatalogService(
        service_client=CatalogClient(),
        auth_admin=auth,
    )
    try:
        populated.delete_account("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    except ValueError as exc:
        assert "leave or delete every workspace" in str(exc)
    else:
        raise AssertionError("account deletion should require no memberships")


def test_workspace_member_router_caches_services_by_workspace() -> None:
    created = []

    def factory(workspace_id):
        created.append(workspace_id)
        return {"workspace_id": workspace_id}

    router = WorkspaceMemberServiceRouter(factory)

    first = router.for_workspace("workspace-1")

    assert router.for_workspace("workspace-1") is first
    assert router.for_workspace("workspace-2")["workspace_id"] == "workspace-2"
    assert created == ["workspace-1", "workspace-2"]
