from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "cloudflare_boundary_audit", ROOT / "scripts/audit-cloudflare-boundaries.py"
)
assert SPEC and SPEC.loader
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeCloudflare:
    def __init__(
        self,
        *,
        public_access: bool = False,
        edge_ready: bool = True,
        policy_token_id: str = "token-1",
        proxied: bool = True,
        extra_edge_policy: dict | None = None,
    ) -> None:
        self.public_access = public_access
        self.edge_ready = edge_ready
        self.policy_token_id = policy_token_id
        self.proxied = proxied
        self.extra_edge_policy = extra_edge_policy

    def get(self, path: str, **params: str):
        if path == "/zones":
            return [{"id": "zone-1", "account": {"id": "account-1"}}]
        if path == "/zones/zone-1/dns_records":
            host = params["name"]
            if host == "magpie.example.com":
                return [{
                    "type": "CNAME",
                    "proxied": self.proxied,
                    "content": "tunnel-1.cfargotunnel.com",
                }]
            if self.edge_ready:
                return [{
                    "type": "CNAME",
                    "proxied": self.proxied,
                    "content": "tunnel-1.cfargotunnel.com",
                }]
            return []
        if path == "/accounts/account-1/cfd_tunnel/tunnel-1/configurations":
            return {
                "config": {
                    "ingress": [
                        {"hostname": "magpie.example.com", "service": "http://127.0.0.1:8766"},
                        {"hostname": "magpie-edge.example.com", "service": "http://127.0.0.1:8766"},
                    ]
                }
            }
        if path == "/accounts/account-1/access/apps":
            apps = [{"id": "edge-app", "domain": "magpie-edge.example.com"}]
            if self.public_access:
                apps.append({"id": "public-app", "domain": "magpie.example.com"})
            return apps
        if path == "/accounts/account-1/access/apps/edge-app/policies":
            policies = [{
                "decision": "non_identity",
                "include": [{"service_token": {"token_id": self.policy_token_id}}],
            }]
            if self.extra_edge_policy:
                policies.append(self.extra_edge_policy)
            return policies
        if path == "/accounts/account-1/access/service_tokens":
            return [{"id": "token-1"}] if self.edge_ready else []
        raise AssertionError(f"Unexpected request: {path} {params}")


def _audit(client: FakeCloudflare):
    return MODULE.audit_boundaries(
        client,
        zone_name="example.com",
        public_host="magpie.example.com",
        edge_host="magpie-edge.example.com",
        expected_service="http://127.0.0.1:8766",
    )


def test_cloudflare_boundary_audit_accepts_public_app_and_protected_edge() -> None:
    result = _audit(FakeCloudflare())

    assert result["ready"] is True
    assert all(check["passed"] for check in result["checks"])


def test_cloudflare_boundary_audit_rejects_access_on_customer_hostname() -> None:
    result = _audit(FakeCloudflare(public_access=True))

    assert result["ready"] is False
    check = next(check for check in result["checks"] if check["key"] == "public_without_access")
    assert check["passed"] is False


def test_cloudflare_boundary_audit_reports_missing_edge_boundary() -> None:
    result = _audit(FakeCloudflare(edge_ready=False))

    assert result["ready"] is False
    failed = {check["key"] for check in result["checks"] if not check["passed"]}
    assert {"edge_dns", "edge_ingress", "shared_central_tunnel", "edge_service_token"} <= failed


def test_cloudflare_boundary_audit_rejects_unbound_service_auth_policy() -> None:
    result = _audit(FakeCloudflare(policy_token_id="deleted-token"))

    assert result["ready"] is False
    check = next(check for check in result["checks"] if check["key"] == "edge_service_auth")
    assert check["passed"] is False


def test_cloudflare_boundary_audit_rejects_dns_only_tunnel_records() -> None:
    result = _audit(FakeCloudflare(proxied=False))

    assert result["ready"] is False
    failed = {check["key"] for check in result["checks"] if not check["passed"]}
    assert {"public_dns", "edge_dns"} <= failed


def test_cloudflare_boundary_audit_rejects_human_or_bypass_edge_policy() -> None:
    result = _audit(
        FakeCloudflare(
            extra_edge_policy={
                "decision": "allow",
                "include": [{"everyone": {}}],
            }
        )
    )

    assert result["ready"] is False
    check = next(
        check
        for check in result["checks"]
        if check["key"] == "edge_service_auth_only"
    )
    assert check["passed"] is False
