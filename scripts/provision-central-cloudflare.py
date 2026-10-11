#!/usr/bin/env python3
"""Plan or provision Magpie's dedicated central Cloudflare Tunnel."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_ROOT = "https://api.cloudflare.com/client/v4"


class CloudflareClient:
    def __init__(self, token: str, *, timeout: float = 15.0) -> None:
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None, **params: str) -> Any:
        query = f"?{urlencode(params)}" if params else ""
        data = json.dumps(body).encode() if body is not None else None
        request = Request(
            f"{API_ROOT}{path}{query}",
            method=method,
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=self.timeout) as response:
            payload = json.load(response)
        if not payload.get("success"):
            raise RuntimeError(
                f"Cloudflare API rejected {method} {path}: {payload.get('errors')}"
            )
        return payload.get("result")

    def get(self, path: str, **params: str) -> Any:
        return self.request("GET", path, **params)

    def post(self, path: str, body: Any) -> Any:
        return self.request("POST", path, body)

    def put(self, path: str, body: Any) -> Any:
        return self.request("PUT", path, body)


def _tunnel_id(record: dict[str, Any] | None) -> str:
    content = str((record or {}).get("content") or "")
    suffix = ".cfargotunnel.com"
    return content[: -len(suffix)] if content.endswith(suffix) else ""


def _one(items: list[dict[str, Any]], description: str) -> dict[str, Any] | None:
    if len(items) > 1:
        raise RuntimeError(f"Expected at most one {description}; found {len(items)}")
    return items[0] if items else None


def _desired_ingress(public_host: str, edge_host: str, service: str) -> list[dict[str, str]]:
    return [
        {"hostname": public_host, "service": service},
        {"hostname": edge_host, "service": service},
        {"service": "http_status:404"},
    ]


def inspect_state(
    client: Any,
    *,
    zone_name: str,
    tunnel_name: str,
    public_host: str,
    edge_host: str,
    service: str,
) -> dict[str, Any]:
    zones = client.get("/zones", name=zone_name)
    if len(zones) != 1:
        raise RuntimeError(f"Expected one Cloudflare zone named {zone_name}; found {len(zones)}")
    zone_id = str(zones[0]["id"])
    account_id = str(zones[0]["account"]["id"])

    tunnels = client.get(f"/accounts/{account_id}/cfd_tunnel", is_deleted="false")
    tunnel = _one(
        [item for item in tunnels if item.get("name") == tunnel_name],
        f"active tunnel named {tunnel_name}",
    )
    tunnel_id = str((tunnel or {}).get("id") or "")
    ingress: list[dict[str, Any]] = []
    if tunnel_id:
        configuration = client.get(
            f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations"
        )
        ingress = list(((configuration or {}).get("config") or {}).get("ingress") or [])

    def dns_record(host: str) -> dict[str, Any] | None:
        records = client.get(f"/zones/{zone_id}/dns_records", name=host)
        return _one(records, f"DNS record for {host}")

    desired_ingress = _desired_ingress(public_host, edge_host, service)
    public_record = dns_record(public_host)
    edge_record = dns_record(edge_host)
    desired_target = f"{tunnel_id}.cfargotunnel.com" if tunnel_id else ""
    actions: list[dict[str, str]] = []
    if not tunnel:
        actions.append({"phase": "prepare", "action": "create_tunnel", "target": tunnel_name})
    if ingress != desired_ingress:
        actions.append(
            {"phase": "prepare", "action": "configure_ingress", "target": tunnel_name}
        )
    for host, record in ((public_host, public_record), (edge_host, edge_record)):
        if not desired_target or str((record or {}).get("content") or "") != desired_target:
            actions.append({"phase": "cutover", "action": "upsert_dns", "target": host})
    return {
        "schema": "magpie.cloudflare_provision.v1",
        "zone_id": zone_id,
        "account_id": account_id,
        "tunnel": tunnel,
        "tunnel_id": tunnel_id,
        "ingress": ingress,
        "desired_ingress": desired_ingress,
        "public_record": public_record,
        "edge_record": edge_record,
        "actions": actions,
    }


def _validate_managed_ingress(state: dict[str, Any], public_host: str, edge_host: str) -> None:
    allowed = {public_host, edge_host, ""}
    unknown = [rule for rule in state["ingress"] if str(rule.get("hostname") or "") not in allowed]
    if unknown:
        raise RuntimeError("The existing central tunnel has unmanaged ingress rules; refusing to replace them")


def _write_secret(path: Path, value: str, *, replace: bool) -> None:
    if path.exists() and not replace:
        raise RuntimeError(f"Token file already exists: {path}; use --replace-token-file")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(descriptor, f"{value}\n".encode())
    finally:
        os.close(descriptor)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def apply_prepare(
    client: Any,
    state: dict[str, Any],
    *,
    tunnel_name: str,
    public_host: str,
    edge_host: str,
    token_file: Path,
    replace_token_file: bool,
) -> dict[str, Any]:
    if token_file.exists() and not replace_token_file:
        raise RuntimeError(
            f"Token file already exists: {token_file}; use --replace-token-file"
        )
    if state["tunnel"] and state["tunnel"].get("config_src") != "cloudflare":
        raise RuntimeError("The existing central tunnel is not remotely managed")
    _validate_managed_ingress(state, public_host, edge_host)
    tunnel_id = state["tunnel_id"]
    created = False
    if not tunnel_id:
        tunnel = client.post(
            f"/accounts/{state['account_id']}/cfd_tunnel",
            {"name": tunnel_name, "config_src": "cloudflare"},
        )
        tunnel_id = str(tunnel["id"])
        created = True
    if state["ingress"] != state["desired_ingress"] or created:
        client.put(
            f"/accounts/{state['account_id']}/cfd_tunnel/{tunnel_id}/configurations",
            {"config": {"ingress": state["desired_ingress"]}},
        )
    token = client.get(f"/accounts/{state['account_id']}/cfd_tunnel/{tunnel_id}/token")
    _write_secret(token_file, str(token), replace=replace_token_file)
    return {"phase": "prepare", "tunnel_id": tunnel_id, "token_file": str(token_file)}


def apply_cutover(
    client: Any,
    state: dict[str, Any],
    *,
    public_host: str,
    edge_host: str,
    expected_current_public_tunnel_id: str,
) -> dict[str, Any]:
    tunnel_id = state["tunnel_id"]
    if not tunnel_id:
        raise RuntimeError("Prepare the dedicated central tunnel before cutting over DNS")
    if state["ingress"] != state["desired_ingress"]:
        raise RuntimeError("Central tunnel ingress is not prepared; refusing DNS cutover")
    current_public = _tunnel_id(state["public_record"])
    if current_public not in {expected_current_public_tunnel_id, tunnel_id}:
        raise RuntimeError(
            "Public hostname no longer points to the expected Pi tunnel; refusing DNS cutover"
        )
    target = f"{tunnel_id}.cfargotunnel.com"
    records = (
        (public_host, state["public_record"]),
        (edge_host, state["edge_record"]),
    )
    for host, record in records:
        if record and record.get("type") != "CNAME":
            raise RuntimeError(f"Existing DNS record for {host} is not a CNAME")
    edge_tunnel = _tunnel_id(state["edge_record"])
    if state["edge_record"] and edge_tunnel != tunnel_id:
        raise RuntimeError("Edge hostname already points to another tunnel; refusing DNS cutover")

    changed: list[str] = []
    for host, record in records:
        body = {"type": "CNAME", "name": host, "content": target, "proxied": True, "ttl": 1}
        if str((record or {}).get("content") or "") == target and record.get("proxied") is True:
            continue
        if record:
            client.put(f"/zones/{state['zone_id']}/dns_records/{record['id']}", body)
        else:
            client.post(f"/zones/{state['zone_id']}/dns_records", body)
        changed.append(host)
    return {"phase": "cutover", "tunnel_id": tunnel_id, "dns_changed": changed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zone", required=True)
    parser.add_argument("--public-host", required=True)
    parser.add_argument("--edge-host", required=True)
    parser.add_argument("--service", default="http://127.0.0.1:8766")
    parser.add_argument("--tunnel-name", default="magpie-central")
    parser.add_argument("--token-env", default="CLOUDFLARE_API_TOKEN")
    parser.add_argument("--apply", choices=("prepare", "cutover"))
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--replace-token-file", action="store_true")
    parser.add_argument("--expected-current-public-tunnel-id")
    args = parser.parse_args()
    if not args.service.startswith(("http://127.0.0.1:", "http://localhost:")):
        parser.error("--service must be a loopback HTTP origin")
    if args.tunnel_name == "magpie-pi":
        parser.error("The central tunnel must not reuse the legacy magpie-pi tunnel")
    if args.apply == "prepare" and not args.token_file:
        parser.error("--token-file is required with --apply prepare")
    if args.apply == "cutover" and not args.expected_current_public_tunnel_id:
        parser.error("--expected-current-public-tunnel-id is required with --apply cutover")
    token = os.environ.get(args.token_env, "").strip()
    if not token:
        parser.error(f"{args.token_env} is required")

    client = CloudflareClient(token)
    try:
        state = inspect_state(
            client,
            zone_name=args.zone,
            tunnel_name=args.tunnel_name,
            public_host=args.public_host,
            edge_host=args.edge_host,
            service=args.service,
        )
        if not args.apply:
            print(json.dumps({"mode": "plan", **state}, indent=2, sort_keys=True))
            return 0
        if args.apply == "prepare":
            result = apply_prepare(
                client,
                state,
                tunnel_name=args.tunnel_name,
                public_host=args.public_host,
                edge_host=args.edge_host,
                token_file=args.token_file,
                replace_token_file=args.replace_token_file,
            )
        else:
            result = apply_cutover(
                client,
                state,
                public_host=args.public_host,
                edge_host=args.edge_host,
                expected_current_public_tunnel_id=args.expected_current_public_tunnel_id,
            )
    except Exception as exc:
        print(f"Cloudflare provisioning failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"mode": "applied", **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
