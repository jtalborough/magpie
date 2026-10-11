from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "cloudflare_provision", ROOT / "scripts/provision-central-cloudflare.py"
)
assert SPEC and SPEC.loader
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeCloudflare:
    def __init__(self, *, prepared: bool = False, public_tunnel: str = "pi-tunnel") -> None:
        self.prepared = prepared
        self.public_tunnel = public_tunnel
        self.calls = []

    def get(self, path: str, **params: str):
        self.calls.append(("GET", path, params))
        if path == "/zones":
            return [{"id": "zone-1", "account": {"id": "account-1"}}]
        if path == "/accounts/account-1/cfd_tunnel":
            return [
                {
                    "id": "central-1",
                    "name": "magpie-central",
                    "config_src": "cloudflare",
                }
            ] if self.prepared else []
        if path == "/accounts/account-1/cfd_tunnel/central-1/configurations":
            return {"config": {"ingress": MODULE._desired_ingress(
                "magpie.example.com", "magpie-edge.example.com", "http://127.0.0.1:8766"
            )}}
        if path == "/accounts/account-1/cfd_tunnel/central-1/token":
            return "secret-connector-token"
        if path == "/zones/zone-1/dns_records":
            host = params["name"]
            if host == "magpie.example.com":
                return [{
                    "id": "public-dns", "type": "CNAME", "proxied": True,
                    "content": f"{self.public_tunnel}.cfargotunnel.com",
                }]
            return []
        raise AssertionError(f"Unexpected GET: {path} {params}")

    def post(self, path: str, body):
        self.calls.append(("POST", path, body))
        if path == "/accounts/account-1/cfd_tunnel":
            return {"id": "central-1", "name": "magpie-central"}
        if path == "/zones/zone-1/dns_records":
            return {"id": "edge-dns", **body}
        raise AssertionError(f"Unexpected POST: {path}")

    def put(self, path: str, body):
        self.calls.append(("PUT", path, body))
        return body


def inspect(client: FakeCloudflare):
    return MODULE.inspect_state(
        client,
        zone_name="example.com",
        tunnel_name="magpie-central",
        public_host="magpie.example.com",
        edge_host="magpie-edge.example.com",
        service="http://127.0.0.1:8766",
    )


def test_plan_is_read_only_and_lists_both_phases() -> None:
    client = FakeCloudflare()
    state = inspect(client)

    assert {item["phase"] for item in state["actions"]} == {"prepare", "cutover"}
    assert not any(method in {"POST", "PUT"} for method, _, _ in client.calls)


def test_prepare_creates_tunnel_configures_ingress_and_protects_token(tmp_path) -> None:
    client = FakeCloudflare()
    state = inspect(client)
    token_file = tmp_path / "connector-token"

    result = MODULE.apply_prepare(
        client,
        state,
        tunnel_name="magpie-central",
        public_host="magpie.example.com",
        edge_host="magpie-edge.example.com",
        token_file=token_file,
        replace_token_file=False,
    )

    assert result["tunnel_id"] == "central-1"
    assert token_file.read_text() == "secret-connector-token\n"
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert ("POST", "/accounts/account-1/cfd_tunnel", {
        "name": "magpie-central", "config_src": "cloudflare"
    }) in client.calls
    assert not any("access" in path for _, path, _ in client.calls)


def test_prepare_refuses_to_overwrite_token_file(tmp_path) -> None:
    client = FakeCloudflare(prepared=True)
    state = inspect(client)
    token_file = tmp_path / "connector-token"
    token_file.write_text("keep-me")

    with pytest.raises(RuntimeError, match="already exists"):
        MODULE.apply_prepare(
            client,
            state,
            tunnel_name="magpie-central",
            public_host="magpie.example.com",
            edge_host="magpie-edge.example.com",
            token_file=token_file,
            replace_token_file=False,
        )

    assert token_file.read_text() == "keep-me"
    assert not any(method in {"POST", "PUT"} for method, _, _ in client.calls)


def test_cutover_requires_expected_current_public_tunnel() -> None:
    client = FakeCloudflare(prepared=True, public_tunnel="unexpected")
    state = inspect(client)

    with pytest.raises(RuntimeError, match="expected Pi tunnel"):
        MODULE.apply_cutover(
            client,
            state,
            public_host="magpie.example.com",
            edge_host="magpie-edge.example.com",
            expected_current_public_tunnel_id="pi-tunnel",
        )

    assert not any(method in {"POST", "PUT"} for method, _, _ in client.calls)


def test_cutover_updates_public_dns_and_creates_edge_dns() -> None:
    client = FakeCloudflare(prepared=True)
    state = inspect(client)

    result = MODULE.apply_cutover(
        client,
        state,
        public_host="magpie.example.com",
        edge_host="magpie-edge.example.com",
        expected_current_public_tunnel_id="pi-tunnel",
    )

    assert result["dns_changed"] == ["magpie.example.com", "magpie-edge.example.com"]
    assert any(method == "PUT" and path.endswith("/public-dns") for method, path, _ in client.calls)
    assert any(method == "POST" and path.endswith("/dns_records") for method, path, _ in client.calls)
    assert not any("access" in path for _, path, _ in client.calls)


def test_cutover_validates_all_dns_records_before_writing() -> None:
    client = FakeCloudflare(prepared=True)
    original_get = client.get

    def get_with_bad_edge(path: str, **params: str):
        if path == "/zones/zone-1/dns_records" and params["name"].startswith("magpie-edge"):
            client.calls.append(("GET", path, params))
            return [{"id": "edge-dns", "type": "A", "content": "192.0.2.1"}]
        return original_get(path, **params)

    client.get = get_with_bad_edge
    state = inspect(client)

    with pytest.raises(RuntimeError, match="not a CNAME"):
        MODULE.apply_cutover(
            client,
            state,
            public_host="magpie.example.com",
            edge_host="magpie-edge.example.com",
            expected_current_public_tunnel_id="pi-tunnel",
        )

    assert not any(method in {"POST", "PUT"} for method, _, _ in client.calls)
