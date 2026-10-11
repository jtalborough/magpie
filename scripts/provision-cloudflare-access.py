#!/usr/bin/env python3
"""Plan or provision Magpie's Cloudflare Access hostname boundary."""

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
DEFAULT_APP_NAME = "Magpie edge controllers"
DEFAULT_POLICY_NAME = "Magpie edge service token"
DEFAULT_TOKEN_NAME = "magpie-edge-controller"


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

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)


def _one(items: list[dict[str, Any]], description: str) -> dict[str, Any] | None:
    if len(items) > 1:
        raise RuntimeError(f"Expected at most one {description}; found {len(items)}")
    return items[0] if items else None


def _service_token_ids(policy: dict[str, Any]) -> set[str]:
    token_ids: set[str] = set()
    for rule in policy.get("include") or []:
        token_id = str((rule.get("service_token") or {}).get("token_id") or "")
        if token_id:
            token_ids.add(token_id)
    return token_ids


def _policy_matches_token(policy: dict[str, Any], token_id: str) -> bool:
    return (
        policy.get("decision") in {"non_identity", "service_auth"}
        and token_id in _service_token_ids(policy)
    )


def _desired_policy(token_id: str, policy_name: str) -> dict[str, Any]:
    return {
        "name": policy_name,
        "decision": "non_identity",
        "precedence": 1,
        "include": [{"service_token": {"token_id": token_id}}],
        "exclude": [],
        "require": [],
    }


def inspect_state(
    client: Any,
    *,
    zone_name: str,
    public_host: str,
    edge_host: str,
    token_name: str,
    policy_name: str,
) -> dict[str, Any]:
    zones = client.get("/zones", name=zone_name)
    if len(zones) != 1:
        raise RuntimeError(f"Expected one Cloudflare zone named {zone_name}; found {len(zones)}")
    account_id = str(zones[0]["account"]["id"])

    apps = client.get(f"/accounts/{account_id}/access/apps", per_page="100")
    public_apps = [app for app in apps if app.get("domain") == public_host]
    edge_app = _one(
        [app for app in apps if app.get("domain") == edge_host],
        f"Access application for {edge_host}",
    )
    policies = (
        client.get(f"/accounts/{account_id}/access/apps/{edge_app['id']}/policies")
        if edge_app
        else []
    )
    tokens = client.get(f"/accounts/{account_id}/access/service_tokens", per_page="100")
    service_token = _one(
        [token for token in tokens if token.get("name") == token_name],
        f"service token named {token_name}",
    )
    managed_policy = _one(
        [policy for policy in policies if policy.get("name") == policy_name],
        f"policy named {policy_name}",
    )
    token_id = str((service_token or {}).get("id") or "")
    edge_ready = bool(
        edge_app
        and edge_app.get("type") == "self_hosted"
        and service_token
        and managed_policy
        and _policy_matches_token(managed_policy, token_id)
    )

    actions: list[dict[str, str]] = []
    if not edge_app:
        actions.append({"phase": "prepare", "action": "create_edge_app", "target": edge_host})
    if not service_token:
        actions.append({"phase": "prepare", "action": "create_service_token", "target": token_name})
    if not managed_policy:
        actions.append({"phase": "prepare", "action": "create_service_auth_policy", "target": policy_name})
    elif token_id and not _policy_matches_token(managed_policy, token_id):
        actions.append({"phase": "prepare", "action": "repair_service_auth_policy", "target": policy_name})
    for app in public_apps:
        actions.append(
            {
                "phase": "release-public",
                "action": "delete_public_access_app",
                "target": str(app.get("id") or ""),
            }
        )

    return {
        "schema": "magpie.cloudflare_access_provision.v1",
        "account_id": account_id,
        "public_apps": public_apps,
        "edge_app": edge_app,
        "policies": policies,
        "service_token": service_token,
        "managed_policy": managed_policy,
        "edge_ready": edge_ready,
        "actions": actions,
    }


def _write_credentials(path: Path, client_id: str, client_secret: str, *, replace: bool) -> None:
    if path.exists() and not replace:
        raise RuntimeError(f"Credential file already exists: {path}; use --replace-credential-file")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        f"MAGPIE_CF_ACCESS_CLIENT_ID={client_id}\n"
        f"MAGPIE_CF_ACCESS_CLIENT_SECRET={client_secret}\n"
    )
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        os.write(descriptor, content.encode())
    finally:
        os.close(descriptor)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def apply_prepare(
    client: Any,
    state: dict[str, Any],
    *,
    edge_host: str,
    app_name: str,
    token_name: str,
    policy_name: str,
    token_duration: str,
    credential_file: Path,
    replace_credential_file: bool,
) -> dict[str, Any]:
    existing_token = state["service_token"]
    if existing_token and not credential_file.exists():
        raise RuntimeError(
            "The named service token already exists but its secret cannot be recovered; "
            "provide its existing --credential-file or choose a new --token-name"
        )
    if not existing_token and credential_file.exists() and not replace_credential_file:
        raise RuntimeError(
            f"Credential file already exists: {credential_file}; use --replace-credential-file"
        )
    edge_app = state["edge_app"]
    if edge_app and edge_app.get("type") != "self_hosted":
        raise RuntimeError("The existing edge Access application is not self-hosted")
    unmanaged = [
        policy
        for policy in state["policies"]
        if policy.get("name") != policy_name
    ]
    if unmanaged:
        raise RuntimeError("The edge Access application has unmanaged policies; refusing to replace them")

    created: list[str] = []
    if not edge_app:
        edge_app = client.post(
            f"/accounts/{state['account_id']}/access/apps",
            {
                "name": app_name,
                "domain": edge_host,
                "type": "self_hosted",
                "session_duration": "24h",
                "service_auth_401_redirect": True,
            },
        )
        created.append("edge_app")

    service_token = existing_token
    if not service_token:
        service_token = client.post(
            f"/accounts/{state['account_id']}/access/service_tokens",
            {"name": token_name, "duration": token_duration},
        )
        client_id = str(service_token.get("client_id") or "")
        client_secret = str(service_token.get("client_secret") or "")
        if not client_id or not client_secret:
            raise RuntimeError("Cloudflare did not return the new service-token credentials")
        _write_credentials(
            credential_file,
            client_id,
            client_secret,
            replace=replace_credential_file,
        )
        created.append("service_token")

    token_id = str(service_token.get("id") or "")
    if not token_id:
        raise RuntimeError("Cloudflare service token has no ID")
    desired_policy = _desired_policy(token_id, policy_name)
    policy = state["managed_policy"]
    policy_path = f"/accounts/{state['account_id']}/access/apps/{edge_app['id']}/policies"
    if not policy:
        client.post(policy_path, desired_policy)
        created.append("service_auth_policy")
    elif not _policy_matches_token(policy, token_id):
        client.put(f"{policy_path}/{policy['id']}", desired_policy)
        created.append("service_auth_policy")

    return {
        "phase": "prepare",
        "edge_app_id": str(edge_app["id"]),
        "service_token_id": token_id,
        "credential_file": str(credential_file),
        "created_or_repaired": created,
    }


def apply_release_public(
    client: Any,
    state: dict[str, Any],
    *,
    expected_public_app_ids: list[str],
) -> dict[str, Any]:
    if not state["edge_ready"]:
        raise RuntimeError("The edge Access application and bound service-token policy are not ready")
    actual = sorted(str(app.get("id") or "") for app in state["public_apps"])
    expected = sorted(set(expected_public_app_ids))
    if actual != expected:
        raise RuntimeError(
            f"Public Access applications changed since planning; expected {expected}, found {actual}"
        )
    for app_id in actual:
        client.delete(f"/accounts/{state['account_id']}/access/apps/{app_id}")
    return {"phase": "release-public", "deleted_public_app_ids": actual}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zone", required=True)
    parser.add_argument("--public-host", required=True)
    parser.add_argument("--edge-host", required=True)
    parser.add_argument("--app-name", default=DEFAULT_APP_NAME)
    parser.add_argument("--policy-name", default=DEFAULT_POLICY_NAME)
    parser.add_argument("--token-name", default=DEFAULT_TOKEN_NAME)
    parser.add_argument("--token-duration", default="8760h")
    parser.add_argument("--token-env", default="CLOUDFLARE_API_TOKEN")
    parser.add_argument("--apply", choices=("prepare", "release-public"))
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--replace-credential-file", action="store_true")
    parser.add_argument("--expected-public-app-id", action="append", default=[])
    args = parser.parse_args()
    if args.apply == "prepare" and not args.credential_file:
        parser.error("--credential-file is required with --apply prepare")
    token = os.environ.get(args.token_env, "").strip()
    if not token:
        parser.error(f"{args.token_env} is required")

    client = CloudflareClient(token)
    try:
        state = inspect_state(
            client,
            zone_name=args.zone,
            public_host=args.public_host,
            edge_host=args.edge_host,
            token_name=args.token_name,
            policy_name=args.policy_name,
        )
        if not args.apply:
            print(json.dumps({"mode": "plan", **state}, indent=2, sort_keys=True))
            return 0
        if args.apply == "prepare":
            result = apply_prepare(
                client,
                state,
                edge_host=args.edge_host,
                app_name=args.app_name,
                token_name=args.token_name,
                policy_name=args.policy_name,
                token_duration=args.token_duration,
                credential_file=args.credential_file,
                replace_credential_file=args.replace_credential_file,
            )
        else:
            result = apply_release_public(
                client,
                state,
                expected_public_app_ids=args.expected_public_app_id,
            )
    except Exception as exc:
        print(f"Cloudflare Access provisioning failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"mode": "applied", **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
