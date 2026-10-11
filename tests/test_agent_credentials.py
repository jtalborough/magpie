import hashlib
import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from searchable_storage_led.agent_credentials import SupabaseAgentCredentialService
from searchable_storage_led.api import create_app
from searchable_storage_led.auth import AuthIdentity, InvalidTokenError
from searchable_storage_led.config import AuthConfig, LedConfig
class FakeRestClient:
    def __init__(self) -> None:
        self.rows = []

    def select(self, table, **kwargs):
        assert table == "agent_credentials"
        rows = list(self.rows)
        filters = kwargs.get("filters") or {}
        for key, expression in filters.items():
            if expression == "is.null":
                rows = [row for row in rows if row.get(key) is None]
            elif str(expression).startswith("eq."):
                value = str(expression)[3:]
                rows = [row for row in rows if str(row.get(key)) == value]
        limit = kwargs.get("limit")
        return [dict(row) for row in (rows[:limit] if limit else rows)]

    def insert(self, table, values, **kwargs):
        assert table == "agent_credentials"
        row = {
            "id": f"00000000-0000-0000-0000-{len(self.rows) + 1:012d}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_used_at": None,
            "revoked_at": None,
            **values,
        }
        self.rows.append(row)
        return [dict(row)]

    def update(self, table, values, *, filters, return_rows=True):
        assert table == "agent_credentials"
        selected = self.select(table, filters=filters)
        ids = {row["id"] for row in selected}
        for row in self.rows:
            if row["id"] in ids:
                row.update(values)
        return [dict(row) for row in self.rows if row["id"] in ids] if return_rows else []


def test_supabase_agent_credentials_store_only_hash_and_cache_authentication() -> None:
    client = FakeRestClient()
    service = SupabaseAgentCredentialService(service_client=client, cache_seconds=30)
    token_hash = hashlib.sha256(b"secret-token").hexdigest()

    created = service.issue(
        "workspace-1", "user-1", "Workshop Codex", token_hash, "token1"
    )
    authenticated = service.authenticate(token_hash)
    authenticated_again = service.authenticate(token_hash)

    assert created["name"] == "Workshop Codex"
    assert authenticated["workspace_id"] == "workspace-1"
    assert authenticated_again == authenticated
    assert client.rows[0]["token_hash"] == token_hash
    assert "token_hash" not in created
    assert "token_hash" not in authenticated

    revoked = service.revoke("workspace-1", str(created["id"]))

    assert revoked["revoked_at"]
    assert service.authenticate(token_hash) is None


class FakeAuth:
    def __init__(self, role="owner") -> None:
        self.role = role

    def authenticate(self, token, workspace_id=None, *, allow_unassigned=False):
        if token != "human-token":
            raise InvalidTokenError("invalid session")
        return AuthIdentity("user-1", "j@example.com", "workspace-1", self.role)

    def invalidate_user(self, user_id):
        return None


class FakeMembers:
    def workspace(self):
        return {"id": "workspace-1", "name": "My Home", "owner_id": "user-1"}

    def members(self):
        return []

    def member(self, user_id):
        raise KeyError(user_id)

    def invite(self, email, role, *, redirect_url=""):
        raise NotImplementedError

    def set_role(self, user_id, role):
        raise NotImplementedError

    def remove(self, user_id):
        raise NotImplementedError


class FakeAgentCredentials:
    def __init__(self) -> None:
        self.records = {}

    def authenticate(self, token_hash):
        record = self.records.get(token_hash)
        return dict(record) if record and not record.get("revoked_at") else None

    def list(self, workspace_id):
        return [
            {key: value for key, value in record.items() if key != "token_hash"}
            for record in self.records.values()
            if record["workspace_id"] == workspace_id
        ]

    def issue(self, workspace_id, user_id, name, token_hash, token_hint):
        record = {
            "id": "00000000-0000-0000-0000-000000000001",
            "workspace_id": workspace_id,
            "name": name,
            "token_hint": token_hint,
            "created_by": user_id,
            "created_at": "2026-07-21T00:00:00Z",
            "last_used_at": None,
            "revoked_at": None,
        }
        self.records[token_hash] = record
        return dict(record)

    def revoke(self, workspace_id, credential_id):
        for record in self.records.values():
            if record["workspace_id"] == workspace_id and record["id"] == credential_id:
                record["revoked_at"] = "2026-07-21T01:00:00Z"
                return dict(record)
        return None


def _app(tmp_path, *, role="owner"):
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps({"name": "test", "columns": 1, "rows_bottom_to_top": []}),
        encoding="utf-8",
    )
    credentials = FakeAgentCredentials()
    app = create_app(
        LedConfig(
            pixel_count=0,
            backend="none",
            layout_path=str(layout),
            database_path=str(tmp_path / "inventory.db"),
            public_origin="https://magpie.example",
            auth=AuthConfig(
                mode="supabase",
                supabase_url="https://project.supabase.co",
                workspace_id="workspace-1",
            ),
        ),
        auth_service=FakeAuth(role),
        workspace_member_service=FakeMembers(),
        agent_credential_service=credentials,
    )
    return app, credentials


def _mcp_headers(token):
    return {
        "authorization": f"Bearer {token}",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2025-03-26",
    }


def test_agent_connection_is_shown_once_scoped_to_mcp_and_revocable(tmp_path) -> None:
    app, credentials = _app(tmp_path)
    human = {"authorization": "Bearer human-token"}
    with TestClient(app) as client:
        created = client.post(
            "/workspace/agent-credentials",
            headers=human,
            json={"name": "Workshop Codex"},
        )
        token = created.json()["token"]

        assert created.status_code == 200
        assert created.headers["cache-control"] == "no-store"
        assert token.startswith("magpie_mcp_")
        assert created.json()["shown_once"] is True

        listed = client.get("/workspace/agent-credentials", headers=human).json()
        assert listed["mcp_url"] == "https://magpie.example/mcp/"
        assert listed["credentials"][0]["token_hint"] == token[-6:]
        assert token not in repr(listed)
        assert "token_hash" not in repr(listed)

        mcp = client.post(
            "/mcp/",
            headers=_mcp_headers(token),
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert mcp.status_code == 200
        assert "search_items" in {tool["name"] for tool in mcp.json()["result"]["tools"]}

        assert client.get("/inventory", headers=_mcp_headers(token)).status_code == 401
        assert client.post(
            "/mcp/",
            headers={**_mcp_headers(token), "x-magpie-workspace": "workspace-2"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ).status_code == 403

        credential_id = created.json()["credential"]["id"]
        revoked = client.delete(
            f"/workspace/agent-credentials/{credential_id}", headers=human
        )
        assert revoked.status_code == 200
        assert client.post(
            "/mcp/",
            headers=_mcp_headers(token),
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        ).status_code == 401


def test_only_workspace_managers_can_create_agent_connections(tmp_path) -> None:
    app, _ = _app(tmp_path, role="member")
    with TestClient(app) as client:
        response = client.post(
            "/workspace/agent-credentials",
            headers={"authorization": "Bearer human-token"},
            json={"name": "Personal agent"},
        )

    assert response.status_code == 403
