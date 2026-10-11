import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from searchable_storage_led.api import (
    APP_SHELL_PATHS,
    NETWORK_FIRST_SHELL_PATHS,
    _edge_device_connection,
    create_app,
)
from searchable_storage_led.auth import (
    AuthIdentity,
    InvalidTokenError,
    SupabaseAuthService,
    WorkspaceAccessError,
)
from searchable_storage_led.config import AuthConfig, EdgeConfig, LedConfig, StorageConfig
from searchable_storage_led.edge import (
    SqliteEdgeRepository,
    WorkspaceEdgeRepositoryRouter,
)
from searchable_storage_led.repositories import (
    StorageRepositories,
    WorkspaceRepositoryRouter,
)
from searchable_storage_led.workspace_admin import WorkspaceMemberServiceRouter


TEST_PUBLISHABLE_KEY = "sb_publishable_1234567890abcdefghij_12345678"


class MembershipClient:
    def __init__(self, role="owner") -> None:
        self.role = role
        self.calls = []

    def select(self, table, **kwargs):
        self.calls.append((table, kwargs))
        if not self.role:
            return []
        return [{"workspace_id": "workspace-1", "user_id": "user-1", "role": self.role}]


def auth_http(status=200, body=None):
    payload = body if body is not None else {"id": "user-1", "email": "j@example.com"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/v1/user"
        assert request.headers["apikey"] == "publishable"
        assert request.headers["authorization"] == "Bearer access-token"
        return httpx.Response(status, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_edge_device_connection_marks_stale_controller_offline() -> None:
    status = _edge_device_connection(
        {
            "enrolled": True,
            "credential_scopes": ["commands"],
            "capabilities": ["set_pixels"],
            "last_seen_at": time.time() - 181,
        }
    )

    assert status["connected"] is False
    assert status["command_ready"] is False
    assert status["connection_state"] == "offline"
    assert status["connection_message"] == (
        "Offline; check the device service and network."
    )
    assert status["last_seen_age_seconds"] >= 181


def test_supabase_auth_validates_user_and_workspace_membership() -> None:
    memberships = MembershipClient()
    with auth_http() as client:
        service = SupabaseAuthService(
            url="https://project.supabase.co",
            publishable_key="publishable",
            service_client=memberships,
            workspace_id="workspace-1",
            client=client,
        )
        identity = service.authenticate("access-token")

    assert identity == AuthIdentity(
        user_id="user-1",
        email="j@example.com",
        workspace_id="workspace-1",
        role="owner",
    )
    assert memberships.calls[0][0] == "workspace_members"
    assert memberships.calls[0][1]["filters"] == {
        "workspace_id": "eq.workspace-1",
        "user_id": "eq.user-1",
    }


def test_supabase_auth_rejects_invalid_token() -> None:
    with auth_http(status=401, body={"message": "invalid JWT"}) as client:
        service = SupabaseAuthService(
            url="https://project.supabase.co",
            publishable_key="publishable",
            service_client=MembershipClient(),
            workspace_id="workspace-1",
            client=client,
        )
        with pytest.raises(InvalidTokenError):
            service.authenticate("access-token")


def test_supabase_auth_rejects_nonmember() -> None:
    with auth_http() as client:
        service = SupabaseAuthService(
            url="https://project.supabase.co",
            publishable_key="publishable",
            service_client=MembershipClient(role=""),
            workspace_id="workspace-1",
            client=client,
        )
        with pytest.raises(WorkspaceAccessError):
            service.authenticate("access-token")


class MultiWorkspaceMembershipClient:
    def __init__(self, memberships=None) -> None:
        self.memberships = memberships if memberships is not None else [
            {
                "workspace_id": "workspace-1",
                "user_id": "user-1",
                "role": "owner",
                "created_at": "1",
            },
            {
                "workspace_id": "workspace-2",
                "user_id": "user-1",
                "role": "member",
                "created_at": "2",
            },
        ]

    def select(self, _table, **kwargs):
        filters = kwargs.get("filters") or {}
        workspace = str(filters.get("workspace_id") or "").removeprefix("eq.")
        rows = list(self.memberships)
        if workspace:
            rows = [row for row in rows if row["workspace_id"] == workspace]
        limit = kwargs.get("limit")
        return rows[:limit] if limit else rows


def test_dynamic_auth_selects_only_a_requested_membership() -> None:
    workspace_id = "22222222-2222-2222-2222-222222222222"
    memberships = MultiWorkspaceMembershipClient(
        memberships=[
            {
                "workspace_id": workspace_id,
                "user_id": "user-1",
                "role": "member",
                "created_at": "2",
            }
        ]
    )
    with auth_http() as client:
        service = SupabaseAuthService(
            url="https://project.supabase.co",
            publishable_key="publishable",
            service_client=memberships,
            workspace_id="",
            client=client,
        )
        identity = service.authenticate("access-token", workspace_id)

    assert identity.workspace_id == workspace_id
    assert identity.role == "member"


def test_dynamic_auth_uses_first_membership_when_none_is_requested() -> None:
    workspace_id = "11111111-1111-1111-1111-111111111111"
    memberships = MultiWorkspaceMembershipClient(
        memberships=[
            {
                "workspace_id": workspace_id,
                "user_id": "user-1",
                "role": "owner",
                "created_at": "1",
            }
        ]
    )
    with auth_http() as client:
        service = SupabaseAuthService(
            url="https://project.supabase.co",
            publishable_key="publishable",
            service_client=memberships,
            workspace_id="",
            client=client,
        )
        identity = service.authenticate("access-token")

    assert identity.workspace_id == workspace_id
    assert identity.role == "owner"


def test_dynamic_auth_can_identify_user_before_first_workspace() -> None:
    with auth_http() as client:
        service = SupabaseAuthService(
            url="https://project.supabase.co",
            publishable_key="publishable",
            service_client=MultiWorkspaceMembershipClient(memberships=[]),
            workspace_id="",
            client=client,
        )
        identity = service.authenticate("access-token", allow_unassigned=True)

    assert identity == AuthIdentity(
        user_id="user-1",
        email="j@example.com",
        workspace_id="",
        role="authenticated",
    )


def test_fixed_workspace_auth_rejects_workspace_override() -> None:
    with auth_http() as client:
        service = SupabaseAuthService(
            url="https://project.supabase.co",
            publishable_key="publishable",
            service_client=MembershipClient(),
            workspace_id="workspace-1",
            client=client,
        )
        with pytest.raises(WorkspaceAccessError, match="bound to a different"):
            service.authenticate("access-token", "workspace-2")


def test_supabase_auth_caches_by_token_hash() -> None:
    memberships = MembershipClient()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"id": "user-1"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = SupabaseAuthService(
            url="https://project.supabase.co",
            publishable_key="publishable",
            service_client=memberships,
            workspace_id="workspace-1",
            cache_seconds=30,
            client=client,
        )
        service.authenticate("access-token")
        service.authenticate("access-token")

    assert calls == 1
    assert len(memberships.calls) == 1
    assert "access-token" not in repr(service._cache)


def test_supabase_auth_can_invalidate_cached_user() -> None:
    memberships = MembershipClient(role="member")
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"id": "user-1"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = SupabaseAuthService(
            url="https://project.supabase.co",
            publishable_key="publishable",
            service_client=memberships,
            workspace_id="workspace-1",
            cache_seconds=30,
            client=client,
        )
        service.authenticate("access-token")
        memberships.role = "viewer"
        service.invalidate_user("user-1")
        identity = service.authenticate("access-token")

    assert calls == 2
    assert identity.role == "viewer"


class FakeAuthService:
    def __init__(self) -> None:
        self.invalidated = []

    def authenticate(
        self, token: str, workspace_id=None, *, allow_unassigned=False
    ) -> AuthIdentity:
        if token != "valid-token":
            raise InvalidTokenError("invalid session")
        return AuthIdentity("user-1", "j@example.com", "workspace-1", "owner")

    def invalidate_user(self, user_id):
        self.invalidated.append(user_id)


class ViewerAuthService:
    def __init__(self) -> None:
        self.invalidated = []

    def authenticate(
        self, token: str, workspace_id=None, *, allow_unassigned=False
    ) -> AuthIdentity:
        if token != "viewer-token":
            raise InvalidTokenError("invalid session")
        return AuthIdentity("user-2", "viewer@example.com", "workspace-1", "viewer")

    def invalidate_user(self, user_id):
        self.invalidated.append(user_id)


class AdminAuthService:
    def authenticate(
        self, token: str, workspace_id=None, *, allow_unassigned=False
    ) -> AuthIdentity:
        if token != "admin-token":
            raise InvalidTokenError("invalid session")
        return AuthIdentity("user-4", "admin@example.com", "workspace-1", "admin")


class FakeWorkspaceMemberService:
    def __init__(self):
        self.removed = []

    def workspace(self):
        return {"id": "workspace-1", "name": "My Home", "owner_id": "user-1"}

    def members(self):
        return [
            {
                "workspace_id": "workspace-1",
                "user_id": "user-1",
                "email": "j@example.com",
                "role": "owner",
            }
        ]

    def member(self, user_id):
        role = "admin" if user_id == "admin-1" else "member"
        return {"workspace_id": "workspace-1", "user_id": user_id, "role": role}

    def invite(self, email, role, *, redirect_url=""):
        return {"user_id": "user-3", "email": email, "role": role, "invited": True}

    def set_role(self, user_id, role):
        return {"user_id": user_id, "role": role}

    def remove(self, user_id):
        self.removed.append(user_id)
        return {"user_id": user_id, "role": "member"}


class FakeWorkspaceCatalogService:
    def __init__(self):
        self.deleted = []
        self.renamed = []
        self.deleted_accounts = []

    def workspaces(self, user_id):
        return [{"id": "workspace-1", "name": "My Home", "role": "owner"}]

    def create(self, user_id, name):
        return {"workspace": {"id": "workspace-2", "name": name}}

    def rename(self, user_id, workspace_id, name):
        clean_name = " ".join(name.split())
        self.renamed.append((user_id, workspace_id, clean_name))
        return {"id": workspace_id, "name": clean_name, "owner_id": user_id}

    def delete(self, user_id, workspace_id, confirmation):
        if confirmation != "My Home":
            raise ValueError("workspace name confirmation does not match")
        self.deleted.append((user_id, workspace_id))
        return {
            "workspace": {"id": workspace_id, "name": "My Home"},
            "member_user_ids": [user_id, "user-3"],
            "deleted_photo_objects": 2,
        }

    def delete_account(self, user_id):
        self.deleted_accounts.append(user_id)
        return {"user_id": user_id}


def test_central_auth_middleware_protects_api_but_serves_app_shell(
    tmp_path, monkeypatch
) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []})
    )
    monkeypatch.setenv("TEST_PUBLISHABLE_KEY", TEST_PUBLISHABLE_KEY)
    config = LedConfig(
        pixel_count=1,
        backend="none",
        layout_path=str(layout),
        database_path=str(tmp_path / "app.db"),
        auth=AuthConfig(
            mode="supabase",
            supabase_url="https://project.supabase.co",
            publishable_key_env="TEST_PUBLISHABLE_KEY",
            workspace_id="workspace-1",
        ),
    )
    client = TestClient(
        create_app(
            config,
            auth_service=FakeAuthService(),
            workspace_member_service=FakeWorkspaceMemberService(),
        )
    )

    public_shell = [
        *APP_SHELL_PATHS,
        "/service-worker.js",
    ]
    for path in public_shell:
        assert client.get(path).status_code == 200, path
    service_worker = client.get("/service-worker.js")
    shell_version = client.get("/build").json()["commit"][:16]
    expected_shell = [
        f"{path}?v={shell_version}"
        if path in NETWORK_FIRST_SHELL_PATHS
        else path
        for path in APP_SHELL_PATHS
    ]
    assert (
        f"const APP_SHELL = {json.dumps(expected_shell)};"
        in service_worker.text
    )

    legacy_browse = client.get("/browse", follow_redirects=False)
    assert legacy_browse.status_code == 307
    assert legacy_browse.headers["location"] == "/storage?view=places"
    assert client.get("/kiosk").status_code == 401
    assert client.get("/health").json()["auth"]["mode"] == "supabase"
    assert client.get("/inventory").status_code == 401
    assert client.get("/kiosk/state", params={"wait_seconds": 0}).status_code == 401
    assert (
        client.get("/inventory", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )

    response = client.get(
        "/auth/session", headers={"Authorization": "Bearer valid-token"}
    )
    assert response.status_code == 200
    assert response.json()["identity"] == {
        "user_id": "user-1",
        "email": "j@example.com",
        "workspace_id": "workspace-1",
        "role": "owner",
    }
    assert (
        client.get(
            "/inventory", headers={"Authorization": "Bearer valid-token"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/kiosk/state",
            params={"wait_seconds": 0},
            headers={"Authorization": "Bearer valid-token"},
        ).status_code
        == 200
    )
    assert client.get("/auth/config").json() == {
        "schema": "magpie.auth.config.v1",
        "mode": "supabase",
        "multi_workspace": False,
        "supabase_url": "https://project.supabase.co",
        "publishable_key": TEST_PUBLISHABLE_KEY,
        "edge_url": "",
    }
    workspace = client.get("/workspace", headers={"Authorization": "Bearer valid-token"})
    assert workspace.status_code == 200
    assert workspace.json()["workspace"]["name"] == "My Home"
    invitation = client.post(
        "/workspace/invitations",
        headers={"Authorization": "Bearer valid-token"},
        json={"email": "new@example.com", "role": "member"},
    )
    assert invitation.status_code == 200


def test_local_mode_does_not_require_human_auth(tmp_path) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []})
    )
    config = LedConfig(
        pixel_count=1,
        backend="none",
        layout_path=str(layout),
        database_path=str(tmp_path / "app.db"),
    )
    client = TestClient(create_app(config))

    assert client.get("/inventory").status_code == 200
    assert client.get("/auth/config").json()["mode"] == "none"


def test_workspace_owner_can_delete_with_exact_name_confirmation(
    tmp_path, monkeypatch
) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []})
    )
    monkeypatch.setenv("TEST_PUBLISHABLE_KEY", TEST_PUBLISHABLE_KEY)
    auth = FakeAuthService()
    catalog = FakeWorkspaceCatalogService()
    config = LedConfig(
        pixel_count=1,
        backend="none",
        layout_path=str(layout),
        database_path=str(tmp_path / "app.db"),
        auth=AuthConfig(
            mode="supabase",
            supabase_url="https://project.supabase.co",
            publishable_key_env="TEST_PUBLISHABLE_KEY",
            workspace_id="workspace-1",
        ),
    )
    client = TestClient(
        create_app(
            config,
            auth_service=auth,
            workspace_member_service=FakeWorkspaceMemberService(),
            workspace_catalog_service=catalog,
        )
    )
    headers = {"Authorization": "Bearer valid-token"}

    assert client.delete("/account", headers=headers).status_code == 400

    renamed = client.patch(
        "/workspace", headers=headers, json={"name": "  Main   Workshop "}
    )
    assert renamed.status_code == 200
    assert renamed.json()["workspace"]["name"] == "Main Workshop"
    assert catalog.renamed == [("user-1", "workspace-1", "Main Workshop")]
    assert client.delete("/workspace/membership", headers=headers).status_code == 400

    wrong = client.request(
        "DELETE", "/workspace", headers=headers, json={"confirmation": "my home"}
    )
    assert wrong.status_code == 400
    deleted = client.request(
        "DELETE", "/workspace", headers=headers, json={"confirmation": "My Home"}
    )

    assert deleted.status_code == 200
    assert deleted.json()["deleted_photo_objects"] == 2
    assert catalog.deleted == [("user-1", "workspace-1")]
    assert auth.invalidated == ["user-1", "user-3"]


def test_workspace_member_can_leave_without_admin_help(tmp_path, monkeypatch) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []})
    )
    monkeypatch.setenv("TEST_PUBLISHABLE_KEY", TEST_PUBLISHABLE_KEY)
    auth = ViewerAuthService()
    members = FakeWorkspaceMemberService()
    config = LedConfig(
        pixel_count=1,
        backend="none",
        layout_path=str(layout),
        database_path=str(tmp_path / "app.db"),
        auth=AuthConfig(
            mode="supabase",
            supabase_url="https://project.supabase.co",
            publishable_key_env="TEST_PUBLISHABLE_KEY",
            workspace_id="workspace-1",
        ),
    )
    client = TestClient(
        create_app(
            config,
            auth_service=auth,
            workspace_member_service=members,
            workspace_catalog_service=FakeWorkspaceCatalogService(),
        )
    )

    response = client.delete(
        "/workspace/membership",
        headers={"Authorization": "Bearer viewer-token"},
    )

    assert response.status_code == 200
    assert members.removed == ["user-2"]
    assert auth.invalidated == ["user-2"]


def test_non_owner_cannot_delete_workspace(tmp_path, monkeypatch) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []})
    )
    monkeypatch.setenv("TEST_PUBLISHABLE_KEY", TEST_PUBLISHABLE_KEY)
    config = LedConfig(
        pixel_count=1,
        backend="none",
        layout_path=str(layout),
        database_path=str(tmp_path / "app.db"),
        auth=AuthConfig(
            mode="supabase",
            supabase_url="https://project.supabase.co",
            publishable_key_env="TEST_PUBLISHABLE_KEY",
            workspace_id="workspace-1",
        ),
    )
    client = TestClient(
        create_app(
            config,
            auth_service=ViewerAuthService(),
            workspace_member_service=FakeWorkspaceMemberService(),
            workspace_catalog_service=FakeWorkspaceCatalogService(),
        )
    )

    response = client.request(
        "DELETE",
        "/workspace",
        headers={"Authorization": "Bearer viewer-token"},
        json={"confirmation": "My Home"},
    )

    assert response.status_code == 403


def test_admin_cannot_manage_admin_role(tmp_path, monkeypatch) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []})
    )
    monkeypatch.setenv("TEST_PUBLISHABLE_KEY", TEST_PUBLISHABLE_KEY)
    config = LedConfig(
        pixel_count=1,
        backend="none",
        layout_path=str(layout),
        database_path=str(tmp_path / "app.db"),
        auth=AuthConfig(
            mode="supabase",
            supabase_url="https://project.supabase.co",
            publishable_key_env="TEST_PUBLISHABLE_KEY",
            workspace_id="workspace-1",
        ),
    )
    client = TestClient(
        create_app(
            config,
            auth_service=AdminAuthService(),
            workspace_member_service=FakeWorkspaceMemberService(),
        )
    )
    headers = {"Authorization": "Bearer admin-token"}

    assert client.patch(
        "/workspace/members/admin-1",
        headers=headers,
        json={"role": "member"},
    ).status_code == 403
    assert client.delete("/workspace/members/admin-1", headers=headers).status_code == 403
    assert client.post(
        "/workspace/invitations",
        headers=headers,
        json={"email": "peer@example.com", "role": "admin"},
    ).status_code == 403


def test_viewer_role_can_read_but_cannot_mutate_workspace(tmp_path, monkeypatch) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []})
    )
    monkeypatch.setenv("TEST_PUBLISHABLE_KEY", TEST_PUBLISHABLE_KEY)
    config = LedConfig(
        pixel_count=1,
        backend="none",
        layout_path=str(layout),
        database_path=str(tmp_path / "app.db"),
        auth=AuthConfig(
            mode="supabase",
            supabase_url="https://project.supabase.co",
            publishable_key_env="TEST_PUBLISHABLE_KEY",
            workspace_id="workspace-1",
        ),
    )
    client = TestClient(
        create_app(
            config,
            auth_service=ViewerAuthService(),
            workspace_member_service=FakeWorkspaceMemberService(),
        )
    )
    headers = {"Authorization": "Bearer viewer-token"}

    assert client.get("/inventory", headers=headers).status_code == 200
    assert client.get("/items/labels.pdf", headers=headers).status_code == 400
    denied = client.post("/clear", headers=headers)

    assert denied.status_code == 403
    assert denied.json()["detail"] == "this workspace role has read-only access"


def test_edge_device_endpoint_keeps_separate_auth_boundary(
    tmp_path, monkeypatch
) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []})
    )
    monkeypatch.setenv("TEST_PUBLISHABLE_KEY", TEST_PUBLISHABLE_KEY)
    config = LedConfig(
        pixel_count=1,
        backend="none",
        layout_path=str(layout),
        database_path=str(tmp_path / "app.db"),
        auth=AuthConfig(
            mode="supabase",
            supabase_url="https://project.supabase.co",
            publishable_key_env="TEST_PUBLISHABLE_KEY",
            workspace_id="workspace-1",
        ),
    )
    client = TestClient(
        create_app(
            config,
            auth_service=FakeAuthService(),
            workspace_member_service=FakeWorkspaceMemberService(),
        )
    )

    response = client.post(
        "/edge/devices/main-cabinet/heartbeat",
        json={"version": "test", "capabilities": []},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid edge device credentials"


def test_enrolled_edge_device_can_only_use_voice_bridge_paths(
    tmp_path, monkeypatch
) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []})
    )
    monkeypatch.setenv("TEST_PUBLISHABLE_KEY", TEST_PUBLISHABLE_KEY)
    config = LedConfig(
        pixel_count=1,
        backend="none",
        layout_path=str(layout),
        database_path=str(tmp_path / "app.db"),
        auth=AuthConfig(
            mode="supabase",
            supabase_url="https://project.supabase.co",
            publishable_key_env="TEST_PUBLISHABLE_KEY",
            workspace_id="workspace-1",
        ),
        edge=EdgeConfig(mode="central", target_device_id="main-cabinet"),
    )
    repository = SqliteEdgeRepository(
        tmp_path / "edge.db",
        workspace_id="workspace-1",
    )
    token = "cabinet-voice-token"
    repository.set_credential(
        "main-cabinet",
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        token_hint=token[-6:],
        scopes=["screen", "voice"],
    )
    client = TestClient(
        create_app(
            config,
            edge_repository=repository,
            auth_service=FakeAuthService(),
            workspace_member_service=FakeWorkspaceMemberService(),
        )
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Magpie-Device-ID": "main-cabinet",
    }

    requests = [
        ("/assistant/chat", {"text": "clear lights", "source": "voice"}),
        ("/assistant/voice/confirm", None),
        ("/assistant/voice/cancel", None),
        ("/assistant/undo", None),
        ("/voice/attempts", {}),
        ("/voice/state", {"state": "waiting", "message": "Waiting for Magpie"}),
    ]
    for path, body in requests:
        response = client.post(path, headers=headers, json=body)
        assert response.status_code == 200, (path, response.text)
    revise = client.post(
        "/assistant/revise",
        headers=headers,
        json={"text": "use a larger bin", "source": "voice"},
    )
    assert revise.status_code == 409
    assert "no pending Magpie proposal" in revise.json()["detail"]

    assert client.get(
        "/kiosk/state?wait_seconds=0", headers=headers
    ).status_code == 200
    assert client.post(
        "/screen/heartbeat", headers=headers, json={"screen_id": "main-display"}
    ).status_code == 200

    screen_token = "screen-only-token"
    repository.set_credential(
        "workshop-screen",
        token_hash=hashlib.sha256(screen_token.encode()).hexdigest(),
        token_hint=screen_token[-6:],
        scopes=["screen"],
    )
    screen_headers = {
        "Authorization": f"Bearer {screen_token}",
        "X-Magpie-Device-ID": "workshop-screen",
    }
    for action in ("confirm", "cancel", "undo", "clear"):
        assert client.post(
            f"/kiosk/actions/{action}", headers=screen_headers
        ).status_code == 200
    assert client.post(
        "/assistant/voice/confirm", headers=screen_headers
    ).status_code == 403
    assert client.post(
        "/assistant/chat",
        headers=screen_headers,
        json={"text": "find screws", "source": "voice"},
    ).status_code == 403

    command_only_token = "command-only-token"
    repository.set_credential(
        "command-controller",
        token_hash=hashlib.sha256(command_only_token.encode()).hexdigest(),
        token_hint=command_only_token[-6:],
        scopes=["commands"],
    )
    assert client.post(
        "/assistant/chat",
        headers={
            "Authorization": f"Bearer {command_only_token}",
            "X-Magpie-Device-ID": "command-controller",
        },
        json={"text": "clear lights", "source": "voice"},
    ).status_code == 403

    assert client.get("/inventory", headers=headers).status_code == 401
    assert client.post("/clear", headers=headers).status_code == 401
    assert client.post(
        "/assistant/chat",
        headers={
            "Authorization": "Bearer wrong-token",
            "X-Magpie-Device-ID": "main-cabinet",
        },
        json={"text": "clear lights", "source": "voice"},
    ).status_code == 401
    assert client.post(
        "/assistant/chat",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Magpie-Device-ID": "invalid/device",
        },
        json={"text": "clear lights", "source": "voice"},
    ).status_code == 400


def test_workspace_owner_can_enroll_rotate_and_revoke_edge_device(
    tmp_path, monkeypatch
) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []})
    )
    monkeypatch.setenv("TEST_PUBLISHABLE_KEY", TEST_PUBLISHABLE_KEY)
    config = LedConfig(
        pixel_count=1,
        backend="none",
        layout_path=str(layout),
        database_path=str(tmp_path / "app.db"),
        auth=AuthConfig(
            mode="supabase",
            supabase_url="https://project.supabase.co",
            publishable_key_env="TEST_PUBLISHABLE_KEY",
            workspace_id="workspace-1",
        ),
    )
    repository = SqliteEdgeRepository(
        tmp_path / "edge.db",
        workspace_id="workspace-1",
    )
    client = TestClient(
        create_app(
            config,
            edge_repository=repository,
            auth_service=FakeAuthService(),
            workspace_member_service=FakeWorkspaceMemberService(),
        )
    )
    owner = {"Authorization": "Bearer valid-token"}

    enrolled = client.post(
        "/workspace/devices",
        headers=owner,
        json={
            "device_id": "garage-kiosk",
            "scopes": ["commands", "screen", "voice"],
        },
    )
    assert enrolled.status_code == 200
    first_token = enrolled.json()["token"]
    assert enrolled.json()["shown_once"] is True
    assert first_token
    assert first_token not in repr(repository.status())
    assert client.post(
        "/workspace/devices",
        headers=owner,
        json={"device_id": "garage-kiosk"},
    ).status_code == 409

    heartbeat = client.post(
        "/edge/devices/garage-kiosk/heartbeat",
        headers={"Authorization": f"Bearer {first_token}"},
        json={"version": "v1", "capabilities": ["screen"]},
    )
    assert heartbeat.status_code == 200
    listed = client.get("/workspace/devices", headers=owner).json()
    assert listed["can_manage"] is True
    assert listed["devices"][0]["device_id"] == "garage-kiosk"
    assert listed["devices"][0]["enrolled"] is True
    assert listed["devices"][0]["credential_hint"] == first_token[-6:]
    assert listed["devices"][0]["credential_scopes"] == [
        "commands",
        "screen",
        "voice",
    ]
    assert listed["devices"][0]["connected"] is True
    assert listed["devices"][0]["command_ready"] is False
    assert listed["devices"][0]["connection_state"] == "connected"
    assert listed["devices"][0]["connection_message"] == (
        "Connected; waiting for light-controller capability."
    )
    assert listed["devices"][0]["last_seen_age_seconds"] >= 0
    assert "credential_hash" not in repr(listed)
    assert "token" not in repr(listed)

    device_headers = {
        "Authorization": f"Bearer {first_token}",
        "X-Magpie-Device-ID": "garage-kiosk",
    }
    voice = client.post(
        "/voice/state",
        headers=device_headers,
        json={
            "state": "waiting",
            "message": "Waiting for Magpie",
            "device_id": "spoofed-device",
        },
    )
    screen = client.post(
        "/screen/heartbeat",
        headers=device_headers,
        json={"screen_id": "garage-screen", "status": "active"},
    )
    assert voice.status_code == 200
    assert voice.json()["voice"]["device_id"] == "garage-kiosk"
    assert screen.status_code == 200
    assert screen.json()["screen"]["device_id"] == "garage-kiosk"

    active_device = client.get("/workspace/devices", headers=owner).json()[
        "devices"
    ][0]
    assert active_device["reported_capabilities"] == ["screen"]
    assert active_device["runtime_capabilities"] == [
        "kiosk_display",
        "voice_assistant",
    ]
    assert active_device["capabilities"] == [
        "kiosk_display",
        "screen",
        "voice_assistant",
    ]
    assert active_device["service_readiness"]["voice"]["ready"] is True
    assert active_device["service_readiness"]["screen"]["ready"] is True

    observed_at = time.time()
    monkeypatch.setattr(
        "searchable_storage_led.api.time.time",
        lambda: observed_at + 60,
    )
    stale_device = client.get("/workspace/devices", headers=owner).json()["devices"][0]
    assert stale_device["connected"] is True
    assert stale_device["runtime_capabilities"] == []
    assert stale_device["service_readiness"]["voice"]["ready"] is False
    assert stale_device["service_readiness"]["screen"]["ready"] is False

    human_spoof = client.post(
        "/voice/state",
        headers=owner,
        json={
            "state": "waiting",
            "message": "Not a device heartbeat",
            "device_id": "garage-kiosk",
        },
    )
    assert human_spoof.status_code == 200
    assert human_spoof.json()["voice"]["device_id"] == ""
    after_spoof = client.get("/workspace/devices", headers=owner).json()["devices"][0]
    assert after_spoof["runtime_capabilities"] == []

    rotated = client.post(
        "/workspace/devices/garage-kiosk/rotate",
        headers=owner,
    )
    assert rotated.status_code == 200
    second_token = rotated.json()["token"]
    assert second_token != first_token
    assert rotated.json()["device"]["credential_scopes"] == [
        "commands",
        "screen",
        "voice",
    ]
    assert client.post(
        "/edge/devices/garage-kiosk/heartbeat",
        headers={"Authorization": f"Bearer {first_token}"},
        json={},
    ).status_code == 401
    assert client.post(
        "/edge/devices/garage-kiosk/heartbeat",
        headers={"Authorization": f"Bearer {second_token}"},
        json={},
    ).status_code == 200

    revoked = client.delete("/workspace/devices/garage-kiosk", headers=owner)
    assert revoked.status_code == 200
    assert revoked.json()["device"]["enrolled"] is False
    assert client.post(
        "/edge/devices/garage-kiosk/heartbeat",
        headers={"Authorization": f"Bearer {second_token}"},
        json={},
    ).status_code == 401

    viewer_client = TestClient(
        create_app(
            config,
            edge_repository=repository,
            auth_service=ViewerAuthService(),
            workspace_member_service=FakeWorkspaceMemberService(),
        )
    )
    viewer = {"Authorization": "Bearer viewer-token"}
    assert viewer_client.get("/workspace/devices", headers=viewer).json()[
        "can_manage"
    ] is False
    assert viewer_client.post(
        "/workspace/devices",
        headers=viewer,
        json={"device_id": "attic-screen"},
    ).status_code == 403


def test_workspace_owner_pairs_device_without_receiving_permanent_token(
    tmp_path, monkeypatch
) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []})
    )
    monkeypatch.setenv("TEST_PUBLISHABLE_KEY", TEST_PUBLISHABLE_KEY)
    config = LedConfig(
        pixel_count=1,
        backend="none",
        layout_path=str(layout),
        database_path=str(tmp_path / "app.db"),
        auth=AuthConfig(
            mode="supabase",
            supabase_url="https://project.supabase.co",
            publishable_key_env="TEST_PUBLISHABLE_KEY",
            workspace_id="workspace-1",
        ),
    )
    repository = SqliteEdgeRepository(
        tmp_path / "edge.db",
        workspace_id="workspace-1",
    )
    client = TestClient(
        create_app(
            config,
            edge_repository=repository,
            auth_service=FakeAuthService(),
            workspace_member_service=FakeWorkspaceMemberService(),
        )
    )
    owner = {"Authorization": "Bearer valid-token"}

    pairing = client.post(
        "/workspace/devices/pairing",
        headers=owner,
        json={"device_id": "main-cabinet", "scopes": ["commands", "voice"]},
    )
    assert pairing.status_code == 200
    assert pairing.headers["cache-control"] == "no-store"
    payload = pairing.json()
    pairing_code = payload["pairing_code"]
    assert payload["workspace_id"] == "workspace-1"
    assert payload["shown_once"] is True
    assert "token" not in payload
    listed = client.get("/workspace/devices", headers=owner).json()["devices"][0]
    assert listed["pairing_pending"] is True
    assert listed["enrolled"] is False
    assert listed["connected"] is False
    assert listed["connection_state"] == "pairing"
    assert listed["connection_message"] == "Run the setup command on this device."
    assert pairing_code not in repr(listed)

    wrong = client.post(
        "/edge/devices/main-cabinet/pair",
        json={"pairing_code": "wrong-code-value-1"},
    )
    assert wrong.status_code == 401
    exchanged = client.post(
        "/edge/devices/main-cabinet/pair",
        json={"pairing_code": pairing_code},
    )
    assert exchanged.status_code == 200
    assert exchanged.headers["cache-control"] == "no-store"
    token = exchanged.json()["token"]
    assert token
    assert token not in repr(repository.status())
    assert client.post(
        "/edge/devices/main-cabinet/pair",
        json={"pairing_code": pairing_code},
    ).status_code == 401
    assert client.post(
        "/edge/devices/main-cabinet/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"version": "paired", "capabilities": ["set_pixels"]},
    ).status_code == 200
    connected = client.get("/workspace/devices", headers=owner).json()["devices"][0]
    assert connected["connected"] is True
    assert connected["command_ready"] is True
    assert connected["connection_state"] == "connected"
    assert connected["connection_message"] == "Connected."

    replacement = client.post(
        "/workspace/devices/main-cabinet/pairing",
        headers=owner,
    )
    assert replacement.status_code == 200
    assert replacement.headers["cache-control"] == "no-store"
    assert "token" not in replacement.json()
    assert client.post(
        "/edge/devices/main-cabinet/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    ).status_code == 200
    replaced = client.post(
        "/edge/devices/main-cabinet/pair",
        json={"pairing_code": replacement.json()["pairing_code"]},
    )
    assert replaced.status_code == 200
    assert replaced.json()["token"] != token
    assert client.post(
        "/edge/devices/main-cabinet/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    ).status_code == 401


def test_workspace_device_enrollment_rejects_empty_or_unknown_scopes(
    tmp_path, monkeypatch
) -> None:
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []})
    )
    monkeypatch.setenv("TEST_PUBLISHABLE_KEY", TEST_PUBLISHABLE_KEY)
    config = LedConfig(
        pixel_count=1,
        backend="none",
        layout_path=str(layout),
        database_path=str(tmp_path / "app.db"),
        auth=AuthConfig(
            mode="supabase",
            supabase_url="https://project.supabase.co",
            publishable_key_env="TEST_PUBLISHABLE_KEY",
            workspace_id="workspace-1",
        ),
    )
    client = TestClient(
        create_app(
            config,
            edge_repository=SqliteEdgeRepository(
                tmp_path / "edge.db", workspace_id="workspace-1"
            ),
            auth_service=FakeAuthService(),
            workspace_member_service=FakeWorkspaceMemberService(),
        )
    )
    owner = {"Authorization": "Bearer valid-token"}

    for scopes in ([], ["commands", "admin"]):
        response = client.post(
            "/workspace/devices",
            headers=owner,
            json={"device_id": "invalid-device", "scopes": scopes},
        )
        assert response.status_code == 400


WORKSPACE_ONE = "11111111-1111-1111-1111-111111111111"
WORKSPACE_TWO = "22222222-2222-2222-2222-222222222222"
WORKSPACE_NEW = "33333333-3333-3333-3333-333333333333"


class DynamicAuthService:
    def __init__(self) -> None:
        self.memberships = {
            "existing-token": {
                WORKSPACE_ONE: "owner",
                WORKSPACE_TWO: "member",
            },
            "new-token": {},
        }
        self.invalidated = []

    def authenticate(
        self,
        token: str,
        workspace_id=None,
        *,
        allow_unassigned=False,
    ) -> AuthIdentity:
        if token not in self.memberships:
            raise InvalidTokenError("invalid session")
        memberships = self.memberships[token]
        selected = workspace_id or next(iter(memberships), "")
        if selected not in memberships:
            if not selected and allow_unassigned:
                return AuthIdentity(
                    "new-user",
                    "new@example.com",
                    "",
                    "authenticated",
                )
            raise WorkspaceAccessError("workspace access denied")
        return AuthIdentity(
            "existing-user" if token == "existing-token" else "new-user",
            "existing@example.com" if token == "existing-token" else "new@example.com",
            selected,
            memberships[selected],
        )

    def invalidate_user(self, user_id):
        self.invalidated.append(user_id)


class DynamicCatalogService:
    def __init__(self, auth_service) -> None:
        self.auth_service = auth_service
        self.deleted_accounts = []

    def workspaces(self, user_id):
        token = "existing-token" if user_id == "existing-user" else "new-token"
        return [
            {"id": workspace_id, "name": f"Storage {index}", "role": role}
            for index, (workspace_id, role) in enumerate(
                self.auth_service.memberships[token].items(),
                start=1,
            )
        ]

    def create(self, user_id, name):
        assert user_id == "new-user"
        self.auth_service.memberships["new-token"][WORKSPACE_NEW] = "owner"
        return {
            "schema": "magpie.workspace_provision.v1",
            "created": True,
            "workspace": {"id": WORKSPACE_NEW, "name": name},
            "owner": {"user_id": user_id, "role": "owner"},
        }

    def delete_account(self, user_id):
        assert user_id == "new-user"
        assert not self.auth_service.memberships["new-token"]
        self.deleted_accounts.append(user_id)
        return {"user_id": user_id}


class DynamicMemberService(FakeWorkspaceMemberService):
    def __init__(self, workspace_id):
        self.workspace_id = workspace_id

    def workspace(self):
        return {
            "id": self.workspace_id,
            "name": f"Storage {self.workspace_id[-1]}",
            "owner_id": "existing-user",
        }


def dynamic_auth_client(
    tmp_path,
    *,
    edge_machine_url: str = "https://magpie-edge.example.com",
):
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "software", "columns": 1, "rows_bottom_to_top": []})
    )
    auth = DynamicAuthService()

    def repositories(workspace_id):
        sqlite = StorageRepositories.sqlite(tmp_path / f"{workspace_id}.db")
        return StorageRepositories(
            inventory=sqlite.inventory,
            locations=sqlite.locations,
            backend="supabase",
            workspace_id=workspace_id,
        )

    repository_router = WorkspaceRepositoryRouter(repositories, backend="supabase")
    edge_router = WorkspaceEdgeRepositoryRouter(
        lambda workspace_id: SqliteEdgeRepository(
            tmp_path / f"{workspace_id}-edge.db",
            workspace_id=workspace_id,
        ),
        repository_router.current_workspace_id,
    )
    member_router = WorkspaceMemberServiceRouter(
        lambda workspace_id: DynamicMemberService(workspace_id)
    )
    config = LedConfig(
        pixel_count=0,
        backend="none",
        layout_path=str(layout),
        database_path=str(tmp_path / "unused.db"),
        storage=StorageConfig(
            backend="supabase",
            supabase_url="https://project.supabase.co",
            workspace_id="",
        ),
        auth=AuthConfig(
            mode="supabase",
            supabase_url="https://project.supabase.co",
            workspace_id="",
            multi_workspace=True,
        ),
        edge=EdgeConfig(mode="workspace", machine_url=edge_machine_url),
    ).validated()
    client = TestClient(
        create_app(
            config,
            repository_router=repository_router,
            edge_repository=edge_router,
            auth_service=auth,
            workspace_member_router=member_router,
            workspace_catalog_service=DynamicCatalogService(auth),
        )
    )
    return client, auth, repository_router


def test_dynamic_api_routes_storage_by_authenticated_workspace(tmp_path) -> None:
    client, _auth, router = dynamic_auth_client(tmp_path)
    base = {"Authorization": "Bearer existing-token"}
    first = {**base, "X-Magpie-Workspace": WORKSPACE_ONE}
    second = {**base, "X-Magpie-Workspace": WORKSPACE_TWO}

    created = client.post(
        "/locations",
        headers=first,
        json={"type": "room", "label": "Workshop"},
    )

    assert created.status_code == 200
    assert [
        row["label"]
        for row in client.get("/locations", headers=first).json()["locations"]
    ] == ["Workshop"]
    assert client.get("/locations", headers=second).json() == {
        "roots": [],
        "locations": [],
    }
    assert client.post(
        "/voice/state",
        headers=first,
        json={"state": "listening", "message": "Listening in workshop"},
    ).status_code == 200
    assert client.post(
        "/screen/heartbeat",
        headers=first,
        json={"screen_id": "workshop-kiosk", "status": "active"},
    ).status_code == 200
    assert (
        client.get("/voice/status", headers=first).json()["voice"]["state"]
        == "listening"
    )
    assert (
        client.get("/voice/status", headers=second).json()["voice"]["state"]
        == "starting"
    )
    assert (
        client.get("/screen/status", headers=second).json()["screen"]["status"]
        == "unknown"
    )
    assert (
        client.get("/workspace", headers=second).json()["workspace"]["id"]
        == WORKSPACE_TWO
    )
    assert router.info()["workspace_id"] == "request-scoped"


def test_dynamic_api_health_reports_request_scoped_workspace(tmp_path) -> None:
    client, _auth, _router = dynamic_auth_client(tmp_path)

    auth = client.get("/health").json()["auth"]

    assert auth["workspace_id"] == "request-scoped"
    assert auth["multi_workspace"] is True


def test_dynamic_device_pairing_requires_machine_only_origin(tmp_path) -> None:
    client, _auth, _router = dynamic_auth_client(tmp_path, edge_machine_url="")
    owner = {
        "Authorization": "Bearer existing-token",
        "X-Magpie-Workspace": WORKSPACE_ONE,
    }

    devices = client.get("/workspace/devices", headers=owner).json()
    assert devices["pairing_available"] is False
    assert devices["pairing_url"] == ""
    assert devices["pairing_message"] == (
        "Device pairing requires a configured machine-only edge origin."
    )

    pairing = client.post(
        "/workspace/devices/pairing",
        headers=owner,
        json={"device_id": "main-cabinet", "scopes": ["commands"]},
    )
    assert pairing.status_code == 503
    assert pairing.json()["detail"] == devices["pairing_message"]


def test_dynamic_api_reports_central_service_product_contract(tmp_path) -> None:
    client, _auth, _router = dynamic_auth_client(tmp_path)
    headers = {
        "Authorization": "Bearer existing-token",
        "X-Magpie-Workspace": WORKSPACE_ONE,
    }

    readiness = client.get("/product/readiness", headers=headers).json()
    checks = {check["key"]: check for check in readiness["checks"]}
    runtime = client.get("/ready").json()["runtime"]

    assert checks["central_service"]["passed"] is True
    assert checks["secure_edge_controller"]["passed"] is False
    assert runtime == {
        "schema": "magpie.runtime.v1",
        "role": "central_service",
        "hardware_owner": False,
        "storage_authority": "supabase",
        "workspace_scope": "request",
        "edge_execution": "workspace_queue",
        "edge_queue_enabled": True,
    }
    capabilities = client.get("/capabilities", headers=headers).json()["features"]
    assert capabilities["workspace_deletion"] is True
    assert capabilities["workspace_rename"] is True
    assert capabilities["workspace_leave"] is True
    assert capabilities["account_deletion"] is True


def test_live_agent_evals_require_manager_role_and_cap_model_calls(tmp_path) -> None:
    client, _auth, _router = dynamic_auth_client(tmp_path)
    member = {
        "Authorization": "Bearer existing-token",
        "X-Magpie-Workspace": WORKSPACE_TWO,
    }
    owner = {
        "Authorization": "Bearer existing-token",
        "X-Magpie-Workspace": WORKSPACE_ONE,
    }

    denied = client.post(
        "/assistant/evals/replay",
        headers=member,
        json={"limit": 500, "kind": "confirmed", "run_model": True},
    )
    assert denied.status_code == 403
    assert "owners and admins" in denied.json()["detail"]

    dry_run = client.post(
        "/assistant/evals/replay",
        headers=member,
        json={"limit": 500, "kind": "confirmed", "run_model": False},
    )
    assert dry_run.status_code == 200
    assert dry_run.json()["effective_limit"] == 500
    assert dry_run.json()["limit_capped"] is False

    live = client.post(
        "/assistant/evals/replay",
        headers=owner,
        json={"limit": 500, "kind": "confirmed", "run_model": True},
    )
    assert live.status_code == 200
    assert live.json()["requested_limit"] == 500
    assert live.json()["effective_limit"] == 25
    assert live.json()["limit_capped"] is True
    assert live.json()["evidence_count"] == 0
    assert live.json()["gate_passed"] is False


def test_dynamic_edge_credentials_are_workspace_scoped(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "MAGPIE_EDGE_TOKENS_JSON",
        json.dumps({"legacy-static-device": "must-not-cross-workspaces"}),
    )
    client, auth, router = dynamic_auth_client(tmp_path)
    auth.memberships["existing-token"][WORKSPACE_TWO] = "owner"
    human = {"Authorization": "Bearer existing-token"}
    first = {**human, "X-Magpie-Workspace": WORKSPACE_ONE}
    second = {**human, "X-Magpie-Workspace": WORKSPACE_TWO}

    enrolled_first = client.post(
        "/workspace/devices",
        headers=first,
        json={
            "device_id": "main-cabinet",
            "scopes": ["commands", "voice", "screen"],
        },
    )
    enrolled_second = client.post(
        "/workspace/devices",
        headers=second,
        json={
            "device_id": "main-cabinet",
            "scopes": ["commands", "voice", "screen"],
        },
    )
    assert enrolled_first.status_code == 200
    assert enrolled_second.status_code == 200
    first_token = enrolled_first.json()["token"]
    second_token = enrolled_second.json()["token"]
    assert first_token != second_token
    first_devices = client.get("/workspace/devices", headers=first).json()
    second_devices = client.get("/workspace/devices", headers=second).json()
    assert first_devices["workspace_id"] == WORKSPACE_ONE
    assert second_devices["workspace_id"] == WORKSPACE_TWO
    assert [device["device_id"] for device in first_devices["devices"]] == [
        "main-cabinet"
    ]
    assert [device["device_id"] for device in second_devices["devices"]] == [
        "main-cabinet"
    ]

    missing_workspace = client.post(
        "/edge/devices/main-cabinet/heartbeat",
        headers={"Authorization": f"Bearer {first_token}"},
        json={"version": "test", "capabilities": ["set_pixels"]},
    )
    assert missing_workspace.status_code == 400
    assert client.get(
        "/edge/devices/main-cabinet/status",
        headers={"Authorization": f"Bearer {first_token}"},
    ).status_code == 400
    crossed = client.post(
        "/edge/devices/main-cabinet/heartbeat",
        headers={
            "Authorization": f"Bearer {first_token}",
            "X-Magpie-Workspace": WORKSPACE_TWO,
        },
        json={"version": "test", "capabilities": ["set_pixels"]},
    )
    assert crossed.status_code == 401
    assert client.get(
        "/edge/devices/main-cabinet/status",
        headers={
            "Authorization": f"Bearer {first_token}",
            "X-Magpie-Workspace": WORKSPACE_TWO,
        },
    ).status_code == 401

    connected = client.post(
        "/edge/devices/main-cabinet/heartbeat",
        headers={
            "Authorization": f"Bearer {first_token}",
            "X-Magpie-Workspace": WORKSPACE_ONE,
        },
        json={"version": "test", "capabilities": ["set_pixels"]},
    )
    assert connected.status_code == 200
    connected_status = client.get(
        "/edge/devices/main-cabinet/status",
        headers={
            "Authorization": f"Bearer {first_token}",
            "X-Magpie-Workspace": WORKSPACE_ONE,
        },
    )
    assert connected_status.status_code == 200
    connected_body = connected_status.json()
    assert connected_body["device"]["version"] == "test"
    assert connected_body["device"]["command_ready"] is True
    assert first_token not in repr(connected_body)
    assert "credential_hash" not in repr(connected_body)
    voice_state = client.post(
        "/voice/state",
        headers={
            "Authorization": f"Bearer {first_token}",
            "X-Magpie-Device-ID": "main-cabinet",
            "X-Magpie-Workspace": WORKSPACE_ONE,
        },
        json={"state": "listening", "message": "Listening"},
    )
    assert voice_state.status_code == 200
    crossed_voice = client.post(
        "/voice/state",
        headers={
            "Authorization": f"Bearer {first_token}",
            "X-Magpie-Device-ID": "main-cabinet",
            "X-Magpie-Workspace": WORKSPACE_TWO,
        },
        json={"state": "listening", "message": "Listening"},
    )
    assert crossed_voice.status_code == 401

    created = client.post(
        "/locations",
        headers=first,
        json={"id": "main-cabinet", "type": "cabinet", "label": "Main cabinet"},
    )
    assert created.status_code == 200
    bound = client.post(
        "/locations/main-cabinet/update",
        headers=first,
        json={"light_target": "main-cabinet", "light_indexes": [0]},
    )
    assert bound.status_code == 200
    devices = client.get("/workspace/devices", headers=first).json()["devices"]
    assert devices[0]["bound_locations"] == [
        {"id": "main-cabinet", "label": "Main cabinet"}
    ]
    light = client.post("/locations/main-cabinet/light", headers=first)
    assert light.status_code == 200
    assert light.json()["lit"] is True
    claimed = client.post(
        "/edge/devices/main-cabinet/commands/claim",
        headers={
            "Authorization": f"Bearer {first_token}",
            "X-Magpie-Workspace": WORKSPACE_ONE,
        },
    )
    assert claimed.status_code == 200
    assert claimed.json()["command"]["kind"] == "set_pixels"
    assert claimed.json()["command"]["payload"]["pixel_count"] == 1

    readiness = client.get("/product/readiness", headers=first).json()
    checks = {check["key"]: check for check in readiness["checks"]}
    assert checks["secure_edge_controller"]["passed"] is True
    assert "targets=main-cabinet" in checks["secure_edge_controller"]["evidence"]
    assert router.info()["workspace_id"] == "request-scoped"


def test_dynamic_edge_pairing_requires_and_honors_workspace_selector(tmp_path) -> None:
    client, auth, _router = dynamic_auth_client(tmp_path)
    auth.memberships["existing-token"][WORKSPACE_TWO] = "owner"
    human = {"Authorization": "Bearer existing-token"}
    first = {**human, "X-Magpie-Workspace": WORKSPACE_ONE}

    pairing = client.post(
        "/workspace/devices/pairing",
        headers=first,
        json={"device_id": "main-cabinet", "scopes": ["commands"]},
    )
    assert pairing.json()["machine_url"] == "https://magpie-edge.example.com"
    code = pairing.json()["pairing_code"]
    assert client.post(
        "/edge/devices/main-cabinet/pair",
        json={"pairing_code": code},
    ).status_code == 400
    assert client.post(
        "/edge/devices/main-cabinet/pair",
        headers={"X-Magpie-Workspace": WORKSPACE_TWO},
        json={"pairing_code": code},
    ).status_code == 401
    paired = client.post(
        "/edge/devices/main-cabinet/pair",
        headers={"X-Magpie-Workspace": WORKSPACE_ONE},
        json={"pairing_code": code},
    )
    assert paired.status_code == 200
    assert paired.json()["token"]


def test_dynamic_lighting_routes_overlapping_pixels_to_each_bound_device(tmp_path) -> None:
    client, _auth, _router = dynamic_auth_client(tmp_path)
    human = {
        "Authorization": "Bearer existing-token",
        "X-Magpie-Workspace": WORKSPACE_ONE,
    }
    tokens = {}
    for device_id in ("cabinet-a", "cabinet-b"):
        enrolled = client.post(
            "/workspace/devices",
            headers=human,
            json={"device_id": device_id, "scopes": ["commands"]},
        )
        assert enrolled.status_code == 200
        tokens[device_id] = enrolled.json()["token"]
        created = client.post(
            "/locations",
            headers=human,
            json={"id": device_id, "type": "cabinet", "label": device_id.title()},
        )
        assert created.status_code == 200
        bound = client.post(
            f"/locations/{device_id}/update",
            headers=human,
            json={"light_target": device_id, "light_indexes": [0]},
        )
        assert bound.status_code == 200

    lit = client.post(
        "/locations/light-colors",
        headers=human,
        json={
            "locations": [
                {"location_id": "cabinet-a", "color": [255, 0, 0]},
                {"location_id": "cabinet-b", "color": [0, 0, 255]},
            ],
            "brightness": 1.0,
            "seconds": 30,
        },
    )
    assert lit.status_code == 200
    assert lit.json()["lit"] is True

    frames = {}
    for device_id, token in tokens.items():
        claim = client.post(
            f"/edge/devices/{device_id}/commands/claim",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Magpie-Workspace": WORKSPACE_ONE,
            },
        )
        assert claim.status_code == 200
        frames[device_id] = claim.json()["command"]["payload"]["pixels"]
    assert frames == {
        "cabinet-a": [[255, 0, 0]],
        "cabinet-b": [[0, 0, 255]],
    }
    raw = client.post(
        "/pixels/0",
        headers=human,
        json={"color": [255, 255, 255]},
    )
    assert raw.status_code == 400
    assert "location-selected device target" in raw.json()["detail"]


def test_client_bootstrap_returns_selected_workspace_and_catalog(tmp_path) -> None:
    client, _auth, _router = dynamic_auth_client(tmp_path)
    response = client.get(
        "/client/bootstrap",
        headers={
            "Authorization": "Bearer existing-token",
            "X-Magpie-Workspace": WORKSPACE_TWO,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "magpie.client_bootstrap.v1"
    assert body["auth"] == {"mode": "supabase", "multi_workspace": True}
    assert body["identity"]["workspace_id"] == WORKSPACE_TWO
    assert body["workspace"] == {
        "id": WORKSPACE_TWO,
        "name": "Storage 2",
        "role": "member",
    }
    assert [workspace["id"] for workspace in body["workspaces"]] == [
        WORKSPACE_ONE,
        WORKSPACE_TWO,
    ]
    assert body["capabilities"]["schema"] == "magpie.capabilities.v1"
    assert body["capabilities"]["storage"]["workspace_id"] == WORKSPACE_TWO


def test_client_bootstrap_admits_first_time_user_without_workspace(tmp_path) -> None:
    client, _auth, _router = dynamic_auth_client(tmp_path)
    response = client.get(
        "/client/bootstrap",
        headers={"Authorization": "Bearer new-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["identity"]["role"] == "authenticated"
    assert body["identity"]["workspace_id"] == ""
    assert body["workspace"] is None
    assert body["workspaces"] == []


def test_dynamic_kiosk_notifications_stay_in_authenticated_workspace(tmp_path) -> None:
    client, _auth, _router = dynamic_auth_client(tmp_path)
    base = {"Authorization": "Bearer existing-token"}
    first = {**base, "X-Magpie-Workspace": WORKSPACE_ONE}
    second = {**base, "X-Magpie-Workspace": WORKSPACE_TWO}
    initial_voice = client.get("/voice/status", headers=first).json()["voice"]
    assert float(initial_voice["updated_at"] or 0) == 0

    with ThreadPoolExecutor(max_workers=1) as executor:
        waiting = executor.submit(
            client.get,
            "/kiosk/state",
            headers=first,
            params={
                "after_id": 0,
                "voice_updated_at": 0,
                "wait_seconds": 1,
            },
        )
        time.sleep(0.05)
        client.post(
            "/voice/state",
            headers=second,
            json={"state": "heard", "message": "Other workspace"},
        )
        time.sleep(0.05)
        assert not waiting.done()
        client.post(
            "/voice/state",
            headers=first,
            json={"state": "processing", "message": "Selected workspace"},
        )
        changed = waiting.result(timeout=1).json()

    assert changed["timed_out"] is False
    assert changed["voice"]["state"] == "processing"


def test_dynamic_api_rejects_unjoined_workspace(tmp_path) -> None:
    client, _auth, _router = dynamic_auth_client(tmp_path)

    response = client.get(
        "/locations",
        headers={
            "Authorization": "Bearer existing-token",
            "X-Magpie-Workspace": WORKSPACE_NEW,
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "workspace access denied"


def test_workspace_qualified_scan_shell_is_public_but_contains_no_storage_data(
    tmp_path,
) -> None:
    client, _auth, _router = dynamic_auth_client(tmp_path)
    headers = {
        "Authorization": "Bearer existing-token",
        "X-Magpie-Workspace": WORKSPACE_ONE,
    }
    client.post(
        "/locations",
        headers=headers,
        json={"type": "box", "label": "Private Parts Box"},
    )

    shell = client.get(f"/scan/{WORKSPACE_ONE}/l/private-parts-box")

    assert shell.status_code == 200
    assert shell.headers["cache-control"] == "public, max-age=86400"
    assert "Private Parts Box" not in shell.text
    assert (
        f'sessionStorage.setItem("magpie.auth.scan.workspace.v1", "{WORKSPACE_ONE}")'
        in shell.text
    )
    assert (
        f'localStorage.setItem("magpie.auth.workspace.v1", "{WORKSPACE_ONE}")'
        in shell.text
    )
    assert shell.text.index(
        f'localStorage.setItem("magpie.auth.workspace.v1", "{WORKSPACE_ONE}")'
    ) > shell.text.index('if (!response.ok)')
    assert (
        'sessionStorage.removeItem("magpie.auth.scan.workspace.v1")'
        in shell.text
    )
    assert 'window.magpieAuthConfig?.mode === "supabase"' in shell.text
    assert (
        f'String(identity?.workspace_id || "") !== "{WORKSPACE_ONE}"'
        in shell.text
    )
    assert "You do not have access to this storage workspace" in shell.text
    assert 'window.fetch("/l/private-parts-box", {cache:"no-store"})' in shell.text
    assert '<script src="/auth.js?v=' in shell.text


def test_concurrent_requests_keep_workspace_context_isolated(tmp_path) -> None:
    client, _auth, router = dynamic_auth_client(tmp_path)
    base = {"Authorization": "Bearer existing-token"}

    def create_many(workspace_id, prefix):
        headers = {**base, "X-Magpie-Workspace": workspace_id}
        for index in range(8):
            response = client.post(
                "/locations",
                headers=headers,
                json={"type": "box", "label": f"{prefix} {index}"},
            )
            assert response.status_code == 200
        return client.get("/locations", headers=headers).json()["locations"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(create_many, WORKSPACE_ONE, "One")
        second = executor.submit(create_many, WORKSPACE_TWO, "Two")
        first_rows = first.result()
        second_rows = second.result()

    assert len(first_rows) == 8
    assert len(second_rows) == 8
    assert all(row["label"].startswith("One ") for row in first_rows)
    assert all(row["label"].startswith("Two ") for row in second_rows)
    assert router.info()["workspace_id"] == "request-scoped"


def test_first_time_user_can_create_then_use_workspace(tmp_path) -> None:
    client, auth, _router = dynamic_auth_client(tmp_path)
    headers = {"Authorization": "Bearer new-token"}

    session = client.get("/auth/session", headers=headers)
    created = client.post("/workspaces", headers=headers, json={"name": "My Home"})

    assert session.json()["identity"]["role"] == "authenticated"
    assert created.status_code == 201
    assert created.json()["workspace"] == {"id": WORKSPACE_NEW, "name": "My Home"}
    assert auth.invalidated == ["new-user"]
    selected = {**headers, "X-Magpie-Workspace": WORKSPACE_NEW}
    assert client.post(
        "/locations",
        headers=selected,
        json={"type": "area", "label": "Garage"},
    ).status_code == 200


def test_user_without_workspaces_can_delete_account(tmp_path) -> None:
    client, auth, _router = dynamic_auth_client(tmp_path)
    headers = {"Authorization": "Bearer new-token"}

    response = client.delete("/account", headers=headers)

    assert response.status_code == 200
    assert response.json()["account"] == {"user_id": "new-user"}
    assert auth.invalidated == ["new-user"]
