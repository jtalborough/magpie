from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "cloudflare_access_provision", ROOT / "scripts/provision-cloudflare-access.py"
)
assert SPEC and SPEC.loader
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeCloudflare:
    def __init__(
        self,
        *,
        edge_app: bool = False,
        service_token: bool = False,
        policy: bool = False,
        public_app_ids: tuple[str, ...] = ("public-app",),
    ) -> None:
        self.edge_app = edge_app
        self.service_token = service_token
        self.policy = policy
        self.public_app_ids = public_app_ids
        self.calls = []

    def get(self, path: str, **params: str):
        self.calls.append(("GET", path, params))
        if path == "/zones":
            return [{"id": "zone-1", "account": {"id": "account-1"}}]
        if path == "/accounts/account-1/access/apps":
            apps = [
                {"id": app_id, "domain": "magpie.example.com", "type": "self_hosted"}
                for app_id in self.public_app_ids
            ]
            if self.edge_app:
                apps.append({
                    "id": "edge-app",
                    "domain": "magpie-edge.example.com",
                    "type": "self_hosted",
                })
            return apps
        if path == "/accounts/account-1/access/apps/edge-app/policies":
            if not self.policy:
                return []
            return [{
                "id": "policy-1",
                "name": MODULE.DEFAULT_POLICY_NAME,
                "decision": "non_identity",
                "include": [{"service_token": {"token_id": "token-1"}}],
            }]
        if path == "/accounts/account-1/access/service_tokens":
            return ([{"id": "token-1", "name": MODULE.DEFAULT_TOKEN_NAME}]
                    if self.service_token else [])
        raise AssertionError(f"Unexpected GET: {path} {params}")

    def post(self, path: str, body):
        self.calls.append(("POST", path, body))
        if path == "/accounts/account-1/access/apps":
            self.edge_app = True
            return {"id": "edge-app", **body}
        if path == "/accounts/account-1/access/service_tokens":
            self.service_token = True
            return {
                "id": "token-1",
                "name": body["name"],
                "client_id": "client-id",
                "client_secret": "client-secret",
            }
        if path == "/accounts/account-1/access/apps/edge-app/policies":
            self.policy = True
            return {"id": "policy-1", **body}
        raise AssertionError(f"Unexpected POST: {path}")

    def put(self, path: str, body):
        self.calls.append(("PUT", path, body))
        return body

    def delete(self, path: str):
        self.calls.append(("DELETE", path, None))
        return {"id": path.rsplit("/", 1)[-1]}


def inspect(client: FakeCloudflare):
    return MODULE.inspect_state(
        client,
        zone_name="example.com",
        public_host="magpie.example.com",
        edge_host="magpie-edge.example.com",
        token_name=MODULE.DEFAULT_TOKEN_NAME,
        policy_name=MODULE.DEFAULT_POLICY_NAME,
    )


def prepare(client: FakeCloudflare, state, credential_file: Path, *, replace: bool = False):
    return MODULE.apply_prepare(
        client,
        state,
        edge_host="magpie-edge.example.com",
        app_name=MODULE.DEFAULT_APP_NAME,
        token_name=MODULE.DEFAULT_TOKEN_NAME,
        policy_name=MODULE.DEFAULT_POLICY_NAME,
        token_duration="8760h",
        credential_file=credential_file,
        replace_credential_file=replace,
    )


def test_plan_is_read_only_and_lists_prepare_and_release() -> None:
    client = FakeCloudflare()
    state = inspect(client)

    assert {action["phase"] for action in state["actions"]} == {"prepare", "release-public"}
    assert not any(method != "GET" for method, _, _ in client.calls)


def test_prepare_creates_edge_boundary_and_protects_credentials(tmp_path) -> None:
    client = FakeCloudflare()
    credential_file = tmp_path / "edge-access.env"

    result = prepare(client, inspect(client), credential_file)

    assert result["created_or_repaired"] == ["edge_app", "service_token", "service_auth_policy"]
    assert credential_file.read_text() == (
        "MAGPIE_CF_ACCESS_CLIENT_ID=client-id\n"
        "MAGPIE_CF_ACCESS_CLIENT_SECRET=client-secret\n"
    )
    assert credential_file.stat().st_mode & 0o777 == 0o600
    assert not any(method == "DELETE" for method, _, _ in client.calls)
    policy_call = next(
        call for call in client.calls
        if call[0] == "POST" and call[1].endswith("/edge-app/policies")
    )
    assert policy_call[2]["decision"] == "non_identity"
    assert policy_call[2]["include"] == [{"service_token": {"token_id": "token-1"}}]


def test_prepare_ready_state_is_idempotent(tmp_path) -> None:
    client = FakeCloudflare(edge_app=True, service_token=True, policy=True)
    credential_file = tmp_path / "edge-access.env"
    credential_file.write_text("existing credentials\n")

    result = prepare(client, inspect(client), credential_file)

    assert result["created_or_repaired"] == []
    assert credential_file.read_text() == "existing credentials\n"
    assert not any(method != "GET" for method, _, _ in client.calls)


def test_prepare_refuses_existing_token_without_recoverable_secret(tmp_path) -> None:
    client = FakeCloudflare(edge_app=True, service_token=True)

    with pytest.raises(RuntimeError, match="cannot be recovered"):
        prepare(client, inspect(client), tmp_path / "missing.env")

    assert not any(method != "GET" for method, _, _ in client.calls)


def test_prepare_refuses_to_overwrite_credentials_before_writing_cloudflare(tmp_path) -> None:
    client = FakeCloudflare()
    credential_file = tmp_path / "edge-access.env"
    credential_file.write_text("keep me\n")

    with pytest.raises(RuntimeError, match="already exists"):
        prepare(client, inspect(client), credential_file)

    assert credential_file.read_text() == "keep me\n"
    assert not any(method != "GET" for method, _, _ in client.calls)


def test_release_refuses_changed_public_application_ids() -> None:
    client = FakeCloudflare(edge_app=True, service_token=True, policy=True)

    with pytest.raises(RuntimeError, match="changed since planning"):
        MODULE.apply_release_public(
            client,
            inspect(client),
            expected_public_app_ids=["different-app"],
        )

    assert not any(method == "DELETE" for method, _, _ in client.calls)


def test_release_deletes_only_expected_public_apps_after_edge_is_ready() -> None:
    client = FakeCloudflare(
        edge_app=True,
        service_token=True,
        policy=True,
        public_app_ids=("public-a", "public-b"),
    )

    result = MODULE.apply_release_public(
        client,
        inspect(client),
        expected_public_app_ids=["public-b", "public-a"],
    )

    assert result["deleted_public_app_ids"] == ["public-a", "public-b"]
    deletes = [path for method, path, _ in client.calls if method == "DELETE"]
    assert deletes == [
        "/accounts/account-1/access/apps/public-a",
        "/accounts/account-1/access/apps/public-b",
    ]


def test_release_refuses_when_edge_boundary_is_not_ready() -> None:
    client = FakeCloudflare(edge_app=True, service_token=True, policy=False)

    with pytest.raises(RuntimeError, match="not ready"):
        MODULE.apply_release_public(
            client,
            inspect(client),
            expected_public_app_ids=["public-app"],
        )

    assert not any(method == "DELETE" for method, _, _ in client.calls)
