#!/usr/bin/env python3
"""Read-only audit of Magpie's Cloudflare customer and edge boundaries."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_ROOT = "https://api.cloudflare.com/client/v4"


class CloudflareClient:
    def __init__(self, token: str, *, timeout: float = 15.0) -> None:
        self.token = token
        self.timeout = timeout

    def get(self, path: str, **params: str) -> Any:
        query = f"?{urlencode(params)}" if params else ""
        request = Request(
            f"{API_ROOT}{path}{query}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urlopen(request, timeout=self.timeout) as response:
            payload = json.load(response)
        if not payload.get("success"):
            raise RuntimeError(f"Cloudflare API rejected GET {path}: {payload.get('errors')}")
        return payload.get("result")


def _check(key: str, passed: bool, evidence: str, next_step: str) -> dict[str, Any]:
    return {
        "key": key,
        "passed": passed,
        "evidence": evidence,
        "next_step": "" if passed else next_step,
    }


def _tunnel_id(record: dict[str, Any] | None) -> str:
    content = str((record or {}).get("content") or "")
    suffix = ".cfargotunnel.com"
    return content[: -len(suffix)] if content.endswith(suffix) else ""


def _proxied_tunnel_record(record: dict[str, Any] | None) -> bool:
    return bool(
        record
        and record.get("type") == "CNAME"
        and record.get("proxied") is True
        and _tunnel_id(record)
    )


def _ingress_service(client: Any, account_id: str, tunnel_id: str, host: str) -> str:
    if not tunnel_id:
        return ""
    result = client.get(f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations")
    ingress = ((result or {}).get("config") or {}).get("ingress") or []
    for rule in ingress:
        if rule.get("hostname") == host:
            return str(rule.get("service") or "")
    return ""


def _service_token_ids(policy: dict[str, Any]) -> set[str]:
    token_ids: set[str] = set()
    for rule in policy.get("include") or []:
        token_id = str((rule.get("service_token") or {}).get("token_id") or "")
        if token_id:
            token_ids.add(token_id)
    return token_ids


def audit_boundaries(
    client: Any,
    *,
    zone_name: str,
    public_host: str,
    edge_host: str,
    expected_service: str,
) -> dict[str, Any]:
    zones = client.get("/zones", name=zone_name)
    if len(zones) != 1:
        raise RuntimeError(f"Expected one Cloudflare zone named {zone_name}; found {len(zones)}")
    zone = zones[0]
    zone_id = str(zone["id"])
    account_id = str(zone["account"]["id"])

    def dns_record(host: str) -> dict[str, Any] | None:
        records = client.get(
            f"/zones/{zone_id}/dns_records",
            type="CNAME",
            name=host,
        )
        return records[0] if len(records) == 1 else None

    public_record = dns_record(public_host)
    edge_record = dns_record(edge_host)
    public_tunnel = _tunnel_id(public_record)
    edge_tunnel = _tunnel_id(edge_record)
    public_service = _ingress_service(client, account_id, public_tunnel, public_host)
    edge_service = _ingress_service(client, account_id, edge_tunnel, edge_host)

    apps = client.get(f"/accounts/{account_id}/access/apps", per_page="100")
    public_apps = [app for app in apps if app.get("domain") == public_host]
    edge_apps = [app for app in apps if app.get("domain") == edge_host]
    edge_policies: list[dict[str, Any]] = []
    for app in edge_apps:
        edge_policies.extend(
            client.get(f"/accounts/{account_id}/access/apps/{app['id']}/policies")
        )
    service_tokens = client.get(
        f"/accounts/{account_id}/access/service_tokens", per_page="100"
    )
    service_token_ids = {str(token.get("id") or "") for token in service_tokens}
    service_auth = [
        policy
        for policy in edge_policies
        if policy.get("decision") in {"non_identity", "service_auth"}
        and bool(_service_token_ids(policy) & service_token_ids)
    ]
    unsafe_edge_policies = [
        policy
        for policy in edge_policies
        if policy.get("decision") not in {"non_identity", "service_auth"}
        or not bool(_service_token_ids(policy) & service_token_ids)
    ]

    checks = [
        _check(
            "public_dns",
            _proxied_tunnel_record(public_record),
            (
                f"{public_host} -> {str((public_record or {}).get('content') or 'missing')}; "
                f"type={str((public_record or {}).get('type') or 'missing')}; "
                f"proxied={str(bool((public_record or {}).get('proxied'))).lower()}"
            ),
            (
                "Create a proxied CNAME from the customer hostname to the central "
                "tunnel; DNS-only records do not provide the intended Cloudflare "
                "boundary."
            ),
        ),
        _check(
            "public_ingress",
            public_service == expected_service,
            f"tunnel={public_tunnel or 'missing'}, service={public_service or 'missing'}",
            f"Route the customer hostname to {expected_service} on the central host.",
        ),
        _check(
            "public_without_access",
            not public_apps,
            f"matching_access_apps={len(public_apps)}",
            "Remove the Access application from the customer hostname; Supabase protects user data.",
        ),
        _check(
            "edge_dns",
            _proxied_tunnel_record(edge_record),
            (
                f"{edge_host} -> {str((edge_record or {}).get('content') or 'missing')}; "
                f"type={str((edge_record or {}).get('type') or 'missing')}; "
                f"proxied={str(bool((edge_record or {}).get('proxied'))).lower()}"
            ),
            (
                "Create a proxied CNAME from the machine hostname to the central "
                "tunnel; Access cannot protect a DNS-only origin."
            ),
        ),
        _check(
            "edge_ingress",
            edge_service == expected_service,
            f"tunnel={edge_tunnel or 'missing'}, service={edge_service or 'missing'}",
            f"Route the machine hostname to {expected_service} on the central host.",
        ),
        _check(
            "shared_central_tunnel",
            bool(public_tunnel) and public_tunnel == edge_tunnel,
            f"public={public_tunnel or 'missing'}, edge={edge_tunnel or 'missing'}",
            "Route both hostnames through the dedicated central-service tunnel.",
        ),
        _check(
            "edge_access_app",
            len(edge_apps) == 1,
            f"matching_access_apps={len(edge_apps)}",
            "Create one Access self-hosted application for the machine hostname.",
        ),
        _check(
            "edge_service_auth",
            bool(service_auth),
            f"bound_service_auth_policies={len(service_auth)}",
            "Bind a Service Auth policy on the edge application to an existing service token.",
        ),
        _check(
            "edge_service_auth_only",
            bool(edge_policies) and not unsafe_edge_policies,
            (
                f"policies={len(edge_policies)}, "
                f"unsafe_or_unbound={len(unsafe_edge_policies)}"
            ),
            (
                "Remove human, bypass, and unbound policies from the machine "
                "hostname; every policy must require a live service token."
            ),
        ),
        _check(
            "edge_service_token",
            bool(service_tokens),
            f"account_service_tokens={len(service_tokens)}",
            "Create a Cloudflare Access service token for edge controllers.",
        ),
    ]
    return {
        "schema": "magpie.cloudflare_boundary_audit.v1",
        "ready": all(check["passed"] for check in checks),
        "zone": zone_name,
        "public_host": public_host,
        "edge_host": edge_host,
        "expected_service": expected_service,
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zone", required=True)
    parser.add_argument("--public-host", required=True)
    parser.add_argument("--edge-host", required=True)
    parser.add_argument("--expected-service", required=True)
    parser.add_argument("--token-env", default="CLOUDFLARE_API_TOKEN")
    args = parser.parse_args()
    token = os.environ.get(args.token_env, "").strip()
    if not token:
        parser.error(f"{args.token_env} is required")
    try:
        result = audit_boundaries(
            CloudflareClient(token),
            zone_name=args.zone,
            public_host=args.public_host,
            edge_host=args.edge_host,
            expected_service=args.expected_service,
        )
    except Exception as exc:
        print(f"Cloudflare boundary audit failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
